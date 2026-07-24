"""实时 OpenTelemetry Trace 与上下文传播测试。"""

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest
from matterloop_core import LoopContext, LoopEvent, LoopEventType, LoopRequest, Plan, PlanStep
from matterloop_observability import (
    CompositeEventPublisher,
    OpenTelemetryModelClient,
    OpenTelemetryTracePublisher,
)


def _provider_and_exporter() -> tuple[Any, Any]:
    """创建仅用于断言的内存 OTel Provider。"""
    sdk_trace = pytest.importorskip("opentelemetry.sdk.trace")
    sdk_export = pytest.importorskip("opentelemetry.sdk.trace.export")
    in_memory = pytest.importorskip("opentelemetry.sdk.trace.export.in_memory_span_exporter")
    provider = sdk_trace.TracerProvider()
    exporter = in_memory.InMemorySpanExporter()
    provider.add_span_processor(sdk_export.SimpleSpanProcessor(exporter))
    return provider, exporter


def _context() -> LoopContext:
    """构造处于第一个执行步骤的运行上下文。"""
    step = PlanStep("查询数据库", executor="worker", step_id="step-1")
    context = LoopContext(LoopRequest("验证实时追踪"))
    context.run_id = "run-live"
    context.current_plan = Plan((step,))
    return context


def test_child_task_inherits_executor_context() -> None:
    """asyncio 创建的数据库任务必须成为 executor Span 的子节点。"""
    provider, exporter = _provider_and_exporter()

    async def scenario() -> None:
        context = _context()
        publisher = OpenTelemetryTracePublisher(provider)
        await publisher.publish(LoopEvent(LoopEventType.LOOP_STARTED, context))
        await publisher.publish(
            LoopEvent(LoopEventType.EXECUTION_DISPATCHED, context, detail="op-1")
        )

        async def database_call() -> None:
            with provider.get_tracer("database").start_as_current_span("db.query"):
                await asyncio.sleep(0)

        await asyncio.create_task(database_call())
        await publisher.publish(
            LoopEvent(LoopEventType.EXECUTION_COMPLETED, context, detail="op-1")
        )
        await publisher.publish(LoopEvent(LoopEventType.LOOP_COMPLETED, context))

    asyncio.run(scenario())

    spans = {span.name: span for span in exporter.get_finished_spans()}
    root = spans["matterloop.run"]
    executor = spans["matterloop.executor"]
    database = spans["db.query"]
    assert executor.parent is not None
    assert executor.parent.span_id == root.get_span_context().span_id
    assert database.parent is not None
    assert database.parent.span_id == executor.get_span_context().span_id
    assert database.get_span_context().trace_id == root.get_span_context().trace_id


def test_model_generation_is_nested_in_live_phase() -> None:
    """模型 generation Span 必须继承当前 executor，而不重建一条离线 Trace。"""
    provider, exporter = _provider_and_exporter()

    class FakeClient:
        async def generate(self, request: Any) -> Any:
            return SimpleNamespace(
                output_text="完成",
                usage=SimpleNamespace(input_tokens=3, output_tokens=2, total_tokens=5),
                response_id="response-1",
                metadata={"model": "fake-model"},
            )

    async def scenario() -> None:
        context = _context()
        publisher = OpenTelemetryTracePublisher(provider)
        await publisher.publish(LoopEvent(LoopEventType.LOOP_STARTED, context))
        await publisher.publish(LoopEvent(LoopEventType.EXECUTION_DISPATCHED, context))
        client = OpenTelemetryModelClient(FakeClient(), provider)
        request = SimpleNamespace(
            metadata={"run_id": context.run_id, "step_id": "step-1", "agent": "worker"},
            messages=(SimpleNamespace(role="user", content="查询", name=None),),
            temperature=0.2,
            max_output_tokens=64,
        )
        response = await client.generate(request)
        assert response.output_text == "完成"
        await publisher.publish(LoopEvent(LoopEventType.EXECUTION_COMPLETED, context))
        await publisher.publish(LoopEvent(LoopEventType.LOOP_COMPLETED, context))

    asyncio.run(scenario())

    spans = {span.name: span for span in exporter.get_finished_spans()}
    generation = spans["generation:worker"]
    executor = spans["matterloop.executor"]
    assert generation.parent is not None
    assert generation.parent.span_id == executor.get_span_context().span_id
    assert generation.attributes["matterloop.run_id"] == "run-live"
    assert generation.attributes["matterloop.model"] == "fake-model"
    assert generation.attributes["matterloop.usage.total_tokens"] == 5
    assert generation.attributes["matterloop.input"] == '[{"content": "查询", "role": "user"}]'


def test_detach_failure_still_ends_spans_and_clears_run() -> None:
    """即使上下文实现异常，结束路径也不能泄漏未导出的 Span。"""
    provider, exporter = _provider_and_exporter()

    async def scenario() -> None:
        context = _context()
        publisher = OpenTelemetryTracePublisher(provider)
        await publisher.publish(LoopEvent(LoopEventType.LOOP_STARTED, context))
        await publisher.publish(LoopEvent(LoopEventType.EXECUTION_DISPATCHED, context))
        detach = publisher._context.detach
        publisher._context.detach = lambda token: (_ for _ in ()).throw(ValueError("wrong task"))
        try:
            await publisher.publish(LoopEvent(LoopEventType.LOOP_COMPLETED, context))
        finally:
            publisher._context.detach = detach
        assert publisher._runs == {}

    asyncio.run(scenario())

    assert {span.name for span in exporter.get_finished_spans()} == {
        "matterloop.run",
        "matterloop.executor",
    }


def test_blocked_resume_restores_a_real_parent_from_checkpoint() -> None:
    """恢复片段必须以 checkpoint 中已导出的 run Span 为真实父节点。"""
    provider, exporter = _provider_and_exporter()

    async def scenario() -> None:
        context = _context()
        blocked_publisher = OpenTelemetryTracePublisher(provider)
        await blocked_publisher.publish(LoopEvent(LoopEventType.LOOP_STARTED, context))
        baggage = pytest.importorskip("opentelemetry.baggage")
        baggage_token = blocked_publisher._context.attach(
            baggage.set_baggage("private", "must-not-enter-checkpoint")
        )
        try:
            await blocked_publisher.prepare_checkpoint(context, (LoopEventType.LOOP_BLOCKED,))
        finally:
            blocked_publisher._context.detach(baggage_token)
        assert "traceparent" in context.propagation_context
        assert "baggage" not in context.propagation_context
        await blocked_publisher.publish(LoopEvent(LoopEventType.LOOP_BLOCKED, context))
        assert blocked_publisher._runs == {}

        resumed_publisher = OpenTelemetryTracePublisher(provider)
        resumed_context = context.snapshot()
        await resumed_publisher.publish(LoopEvent(LoopEventType.LOOP_RESUMED, resumed_context))
        await resumed_publisher.publish(LoopEvent(LoopEventType.LOOP_COMPLETED, resumed_context))

    asyncio.run(scenario())

    roots = [span for span in exporter.get_finished_spans() if span.name == "matterloop.run"]
    assert len(roots) == 2
    assert roots[0].get_span_context().trace_id == roots[1].get_span_context().trace_id
    assert roots[1].parent is not None
    assert roots[1].parent.span_id == roots[0].get_span_context().span_id


def test_composite_publisher_forwards_checkpoint_preparation() -> None:
    """生产预设的组合 publisher 不能吞掉实时追踪的 checkpoint 钩子。"""

    class PreparingPublisher:
        async def publish(self, event: LoopEvent) -> None:
            del event

        async def prepare_checkpoint(
            self, context: LoopContext, event_types: tuple[LoopEventType, ...]
        ) -> None:
            assert event_types == (LoopEventType.LOOP_PAUSED,)
            context.propagation_context["traceparent"] = "prepared"

    async def scenario() -> None:
        context = _context()
        publisher = CompositeEventPublisher((PreparingPublisher(),))
        await publisher.prepare_checkpoint(context, (LoopEventType.LOOP_PAUSED,))
        assert context.propagation_context == {"traceparent": "prepared"}

    asyncio.run(scenario())


def test_failed_run_marks_open_phase_with_original_error() -> None:
    """失败事件要把根和被强制关闭的阶段均标记为 ERROR。"""
    provider, exporter = _provider_and_exporter()

    async def scenario() -> None:
        context = _context()
        publisher = OpenTelemetryTracePublisher(provider)
        await publisher.publish(LoopEvent(LoopEventType.LOOP_STARTED, context))
        await publisher.publish(LoopEvent(LoopEventType.EXECUTION_DISPATCHED, context))
        context.error = "RuntimeError: executor crashed"
        await publisher.publish(LoopEvent(LoopEventType.LOOP_FAILED, context))

    asyncio.run(scenario())

    spans = {span.name: span for span in exporter.get_finished_spans()}
    assert spans["matterloop.run"].status.status_code.name == "ERROR"
    assert spans["matterloop.executor"].status.status_code.name == "ERROR"
    assert spans["matterloop.executor"].status.description == "RuntimeError: executor crashed"


def test_completion_evaluation_closes_before_terminal_event() -> None:
    """验收通过后，completion evaluator 不能把收尾工作计入自己的耗时。"""
    provider, exporter = _provider_and_exporter()

    async def scenario() -> None:
        context = _context()
        publisher = OpenTelemetryTracePublisher(provider)
        await publisher.publish(LoopEvent(LoopEventType.LOOP_STARTED, context))
        await publisher.publish(LoopEvent(LoopEventType.COMPLETION_EVALUATION_STARTED, context))
        await publisher.publish(LoopEvent(LoopEventType.COMPLETION_EVALUATION_COMPLETED, context))
        assert {span.name for span in exporter.get_finished_spans()} == {
            "matterloop.completion_evaluator"
        }
        await publisher.publish(LoopEvent(LoopEventType.LOOP_COMPLETED, context))

    asyncio.run(scenario())


def test_model_call_survives_live_otel_creation_failure() -> None:
    """观测 SDK 故障不得阻断真实模型调用。"""

    class BrokenTracer:
        def start_span(self, name: str) -> Any:
            del name
            raise RuntimeError("otel unavailable")

    class BrokenProvider:
        def get_tracer(self, name: str) -> BrokenTracer:
            del name
            return BrokenTracer()

    class FakeClient:
        async def generate(self, request: Any) -> Any:
            return SimpleNamespace(output_text="仍然执行")

    async def scenario() -> None:
        client = OpenTelemetryModelClient(FakeClient(), BrokenProvider())
        request = SimpleNamespace(metadata={"run_id": "run-live"})
        response = await client.generate(request)
        assert response.output_text == "仍然执行"

    asyncio.run(scenario())
