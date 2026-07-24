"""TracedModelClient 包装行为测试。"""

import asyncio
from collections.abc import Sequence
from types import SimpleNamespace
from typing import Any

import pytest
from matterloop_core import (
    LoopContext,
    LoopEvent,
    LoopEventType,
    LoopRequest,
    Plan,
    PlanStep,
)
from matterloop_observability import (
    BatchingPipeline,
    SpanRecord,
    TraceBuilder,
    TracedModelClient,
    wrap_model_client,
)
from matterloop_observability.pipeline import ExportItem


class _CollectingExporter:
    """记录全部收到批次的导出器。"""

    def __init__(self) -> None:
        self.items: list[ExportItem] = []

    def export(self, batch: Sequence[ExportItem]) -> None:
        self.items.extend(batch)


def _response() -> Any:
    """构造鸭子类型的模型响应。"""
    usage = SimpleNamespace(
        input_tokens=10,
        output_tokens=5,
        total_tokens=15,
        cache_hit_tokens=2,
        cache_miss_tokens=8,
        reasoning_tokens=3,
    )
    return SimpleNamespace(
        output_text="模型输出",
        usage=usage,
        response_id="resp-1",
        metadata={"model": "fake-model"},
    )


def _request(metadata: dict[str, object]) -> Any:
    """构造鸭子类型的模型请求。"""
    message = SimpleNamespace(role=SimpleNamespace(value="user"), content="你好", name=None)
    return SimpleNamespace(
        metadata=metadata,
        messages=(message,),
        temperature=0.3,
        max_output_tokens=128,
    )


class _FakeClient:
    """记录请求并返回固定响应的假模型客户端。"""

    def __init__(self, error: Exception | None = None) -> None:
        self.requests: list[Any] = []
        self._error = error

    async def generate(self, request: Any) -> Any:
        self.requests.append(request)
        if self._error is not None:
            raise self._error
        return _response()


def _open_executor_run(builder: TraceBuilder) -> LoopContext:
    """驱动到执行器跨度打开的事件序列。"""
    step = PlanStep("实现功能", executor="coder", step_id="step-1")
    context = LoopContext(LoopRequest("构建演示目标"))
    context.run_id = "run-1"
    context.current_plan = Plan((step,))
    context.current_step_index = 0
    builder.handle(LoopEvent(LoopEventType.LOOP_STARTED, context))
    builder.handle(LoopEvent(LoopEventType.EXECUTION_DISPATCHED, context, detail="op-1"))
    return context


def _spans(exporter: _CollectingExporter, observation_type: str) -> list[SpanRecord]:
    """按观测类型取出已导出的跨度。"""
    return [
        item
        for item in exporter.items
        if isinstance(item, SpanRecord) and item.observation_type == observation_type
    ]


def test_generation_span_links_to_active_step_span_and_records_usage() -> None:
    """generation 跨度应挂到当前步骤跨度下并记录完整用量。"""
    exporter = _CollectingExporter()
    pipeline = BatchingPipeline(exporter, flush_at=1, flush_interval=0.05)
    builder = TraceBuilder(pipeline)
    try:
        _open_executor_run(builder)
        parent_id = builder.resolve_parent_span_id("run-1", "step-1")
        fake = _FakeClient()
        client = wrap_model_client(fake, builder)

        request = _request({"run_id": "run-1", "step_id": "step-1", "agent": "coder"})
        response = asyncio.run(client.generate(request))
        pipeline.flush()

        assert response.output_text == "模型输出"
        assert fake.requests == [request]
        generation = _spans(exporter, "generation")[-1]
        assert generation.trace_id == "run-1"
        assert generation.parent_span_id == parent_id
        assert generation.name == "generation:coder"
        attributes = generation.attributes
        assert attributes["matterloop.step_id"] == "step-1"
        assert attributes["matterloop.model"] == "fake-model"
        assert attributes["matterloop.response_id"] == "resp-1"
        assert attributes["matterloop.output"] == "模型输出"
        assert attributes["matterloop.input"] == [{"role": "user", "content": "你好"}]
        assert attributes["matterloop.parameters"] == {
            "temperature": 0.3,
            "max_output_tokens": 128,
        }
        for field, expected in (
            ("input_tokens", 10),
            ("output_tokens", 5),
            ("total_tokens", 15),
            ("cache_hit_tokens", 2),
            ("cache_miss_tokens", 8),
            ("reasoning_tokens", 3),
        ):
            assert attributes[f"matterloop.usage.{field}"] == expected
    finally:
        pipeline.shutdown()


def test_missing_run_id_passes_through_without_span() -> None:
    """缺少 run_id 的调用应直接透传且不产生跨度。"""
    exporter = _CollectingExporter()
    pipeline = BatchingPipeline(exporter, flush_at=1, flush_interval=0.05)
    builder = TraceBuilder(pipeline)
    try:
        fake = _FakeClient()
        client = wrap_model_client(fake, builder)

        response = asyncio.run(client.generate(_request({"agent": "coder"})))
        pipeline.flush()

        assert response.output_text == "模型输出"
        assert _spans(exporter, "generation") == []
    finally:
        pipeline.shutdown()


def test_model_error_records_error_span_and_reraises() -> None:
    """模型异常应记录 ERROR 跨度并原样继续抛出。"""
    exporter = _CollectingExporter()
    pipeline = BatchingPipeline(exporter, flush_at=1, flush_interval=0.05)
    builder = TraceBuilder(pipeline)
    try:
        _open_executor_run(builder)
        fake = _FakeClient(error=ValueError("quota exhausted"))
        client = wrap_model_client(fake, builder)

        with pytest.raises(ValueError, match="quota exhausted"):
            asyncio.run(client.generate(_request({"run_id": "run-1", "step_id": "step-1"})))
        pipeline.flush()

        generation = _spans(exporter, "generation")[-1]
        assert generation.level == "ERROR"
        assert generation.status_message is not None
        assert "ValueError" in generation.status_message
        assert "quota exhausted" in generation.status_message
    finally:
        pipeline.shutdown()


def test_without_trace_builder_generation_span_has_no_parent() -> None:
    """仅提供流水线时 generation 跨度应作为无父节点记录。"""
    exporter = _CollectingExporter()
    pipeline = BatchingPipeline(exporter, flush_at=1, flush_interval=0.05)
    try:
        client = TracedModelClient(_FakeClient(), pipeline=pipeline)

        asyncio.run(client.generate(_request({"run_id": "run-1"})))
        pipeline.flush()

        generation = _spans(exporter, "generation")[-1]
        assert generation.parent_span_id is None
        assert generation.name == "generation"
    finally:
        pipeline.shutdown()


def test_traced_model_client_requires_a_trace_sink() -> None:
    """既没有 TraceBuilder 也没有流水线时应在构造时拒绝。"""
    with pytest.raises(ValueError, match="trace_builder or pipeline"):
        TracedModelClient(_FakeClient())
