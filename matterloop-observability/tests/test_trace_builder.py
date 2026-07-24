"""TraceBuilder 跨度树重建测试。"""

import asyncio
from collections.abc import Sequence
from uuid import uuid4

from matterloop_core import (
    ExecutionResult,
    IterationRecord,
    LoopContext,
    LoopEvent,
    LoopEventType,
    LoopRequest,
    Plan,
    PlanStep,
    VerificationResult,
)
from matterloop_observability import BatchingPipeline, Score, SpanRecord, TraceBuilder
from matterloop_observability.pipeline import ExportItem


class _CollectingExporter:
    """记录全部收到批次的导出器。"""

    def __init__(self) -> None:
        self.items: list[ExportItem] = []

    def export(self, batch: Sequence[ExportItem]) -> None:
        self.items.extend(batch)


def _context(run_id: str = "run-1") -> LoopContext:
    """创建带单步计划的运行上下文。"""
    step = PlanStep("实现功能", executor="coder", step_id="step-1")
    context = LoopContext(LoopRequest("构建演示目标"))
    context.run_id = run_id
    context.current_plan = Plan((step,))
    context.current_step_index = 0
    return context


def _record(context: LoopContext, score: float | None = 80.0) -> IterationRecord:
    """创建对应当前计划步骤的迭代证据。"""
    assert context.current_plan is not None
    return IterationRecord(
        cycle=1,
        step_index=0,
        step=context.current_plan.steps[0],
        execution=ExecutionResult("执行输出"),
        verification=VerificationResult(
            passed=True,
            feedback="符合验收",
            score=score,
            evidence=("证据",),
        ),
    )


def _fixture() -> tuple[_CollectingExporter, BatchingPipeline, TraceBuilder]:
    """装配收集型导出器、流水线和 TraceBuilder。"""
    exporter = _CollectingExporter()
    pipeline = BatchingPipeline(exporter, flush_at=1, flush_interval=0.05)
    return exporter, pipeline, TraceBuilder(pipeline)


def _drive_iteration(
    builder: TraceBuilder, context: LoopContext, score: float | None = 80.0
) -> None:
    """驱动一次完整的执行与验证事件序列。"""
    builder.handle(LoopEvent(LoopEventType.LOOP_STARTED, context))
    builder.handle(LoopEvent(LoopEventType.EXECUTION_DISPATCHED, context, detail="op-1"))
    context.pending_execution = ExecutionResult("执行输出")
    builder.handle(LoopEvent(LoopEventType.EXECUTION_COMPLETED, context, detail="op-1"))
    builder.handle(LoopEvent(LoopEventType.VERIFICATION_STARTED, context))
    context.records.append(_record(context, score))
    builder.handle(LoopEvent(LoopEventType.ITERATION_COMPLETED, context))


def _spans(exporter: _CollectingExporter) -> list[SpanRecord]:
    """取出已导出的跨度记录。"""
    return [item for item in exporter.items if isinstance(item, SpanRecord)]


def _scores(exporter: _CollectingExporter) -> list[Score]:
    """取出已导出的评分记录。"""
    return [item for item in exporter.items if isinstance(item, Score)]


def test_trace_builder_rebuilds_span_tree_and_extracts_score() -> None:
    """完整事件流应重建出父子关系正确的跨度树并提取验证评分。"""
    exporter, pipeline, builder = _fixture()
    context = _context()
    try:
        _drive_iteration(builder, context)
        builder.handle(LoopEvent(LoopEventType.LOOP_COMPLETED, context))
        pipeline.flush()

        spans = _spans(exporter)
        assert {span.trace_id for span in spans} == {"run-1"}
        root = next(span for span in spans if span.parent_span_id is None)
        assert root.name == "构建演示目标"
        assert root.observation_type == "chain"
        assert root.attributes["matterloop.goal"] == "构建演示目标"

        executor = next(span for span in spans if span.name == "executor:coder")
        assert executor.parent_span_id == root.span_id
        assert executor.attributes["matterloop.step_id"] == "step-1"
        assert executor.attributes["matterloop.operation_id"] == "op-1"
        assert executor.attributes["matterloop.output"] == "执行输出"

        evaluator = next(span for span in spans if span.observation_type == "evaluator")
        assert evaluator.parent_span_id == root.span_id
        assert evaluator.attributes["matterloop.verification_passed"] is True
        assert evaluator.started_at <= evaluator.ended_at

        iteration = next(span for span in spans if span.name == "iteration:c1:s0")
        assert iteration.parent_span_id == root.span_id
        assert iteration.attributes["matterloop.cycle"] == 1
        assert iteration.attributes["matterloop.attempt"] == 1

        scores = _scores(exporter)
        assert len(scores) == 1
        assert scores[0].name == "verification"
        assert scores[0].value == 0.8
        assert scores[0].step_id == "step-1"
    finally:
        pipeline.shutdown()


def test_trace_builder_publish_supports_async_event_publisher_protocol() -> None:
    """TraceBuilder 应能作为 EventPublisher 被异步发布调用。"""
    exporter, pipeline, builder = _fixture()
    context = _context()
    try:
        asyncio.run(builder.publish(LoopEvent(LoopEventType.LOOP_STARTED, context)))
        asyncio.run(builder.publish(LoopEvent(LoopEventType.LOOP_COMPLETED, context)))
        pipeline.flush()

        assert any(span.parent_span_id is None for span in _spans(exporter))
    finally:
        pipeline.shutdown()


def test_resolve_parent_span_id_tracks_open_spans() -> None:
    """generation 父节点应优先解析到当前打开的步骤跨度。"""
    exporter, pipeline, builder = _fixture()
    del exporter
    context = _context()
    try:
        assert builder.resolve_parent_span_id("run-1", "step-1") is None

        builder.handle(LoopEvent(LoopEventType.LOOP_STARTED, context))
        root_id = builder.resolve_parent_span_id("run-1")
        assert root_id is not None

        builder.handle(LoopEvent(LoopEventType.EXECUTION_DISPATCHED, context, detail="op-1"))
        executor_id = builder.resolve_parent_span_id("run-1", "step-1")
        assert executor_id is not None
        assert executor_id != root_id
        assert builder.resolve_parent_span_id("run-1", "unknown-step") == root_id

        context.pending_execution = ExecutionResult("输出")
        builder.handle(LoopEvent(LoopEventType.EXECUTION_COMPLETED, context, detail="op-1"))
        builder.handle(LoopEvent(LoopEventType.VERIFICATION_STARTED, context))
        evaluator_id = builder.resolve_parent_span_id("run-1", "step-1")
        assert evaluator_id is not None
        assert evaluator_id not in {root_id, executor_id}

        builder.handle(LoopEvent(LoopEventType.LOOP_COMPLETED, context))
        assert builder.resolve_parent_span_id("run-1", "step-1") is None
    finally:
        pipeline.shutdown()


def test_loop_failed_marks_error_and_force_closes_open_spans() -> None:
    """运行失败时未关跨度应被强制关闭，根跨度标记 ERROR。"""
    exporter, pipeline, builder = _fixture()
    context = _context()
    try:
        builder.handle(LoopEvent(LoopEventType.LOOP_STARTED, context))
        builder.handle(LoopEvent(LoopEventType.EXECUTION_DISPATCHED, context, detail="op-1"))
        context.error = "RuntimeError: executor crashed"
        builder.handle(LoopEvent(LoopEventType.LOOP_FAILED, context))
        pipeline.flush()

        spans = _spans(exporter)
        root = next(span for span in spans if span.parent_span_id is None)
        assert root.level == "ERROR"
        assert root.status_message == "RuntimeError: executor crashed"

        executor = next(span for span in spans if span.name == "executor:coder")
        assert executor.level == "ERROR"
        assert executor.status_message is not None
        assert "强制关闭" in executor.status_message
        assert builder.resolve_parent_span_id("run-1") is None
    finally:
        pipeline.shutdown()


def test_retry_closes_each_executor_attempt_before_the_next_dispatch() -> None:
    """失败重试不能覆盖上一尝试的 executor Span。"""
    exporter, pipeline, builder = _fixture()
    context = _context()
    try:
        builder.handle(LoopEvent(LoopEventType.LOOP_STARTED, context))
        context.pending_attempt = 1
        builder.handle(LoopEvent(LoopEventType.EXECUTION_DISPATCHED, context, detail="operation-1"))
        builder.handle(LoopEvent(LoopEventType.COMPONENT_RETRYING, context, detail="2"))
        context.pending_attempt = 2
        builder.handle(LoopEvent(LoopEventType.EXECUTION_DISPATCHED, context, detail="operation-1"))
        builder.handle(LoopEvent(LoopEventType.LOOP_FAILED, context))
        pipeline.flush()

        executor_spans = [span for span in _spans(exporter) if span.name == "executor:coder"]
        assert len(executor_spans) == 2
        assert [span.attributes["matterloop.attempt"] for span in executor_spans] == [1, 2]
        assert executor_spans[0].level == "ERROR"
        assert executor_spans[0].status_message == "执行器调用失败，准备重试"
    finally:
        pipeline.shutdown()


def test_trace_builder_tolerates_missing_plan_and_records() -> None:
    """缺少计划或迭代记录的事件流不应产生异常或泄漏跨度。"""
    exporter, pipeline, builder = _fixture()
    context = LoopContext(LoopRequest("无计划运行"))
    context.run_id = "run-empty"
    try:
        builder.handle(LoopEvent(LoopEventType.LOOP_STARTED, context))
        builder.handle(LoopEvent(LoopEventType.EXECUTION_DISPATCHED, context))
        builder.handle(LoopEvent(LoopEventType.VERIFICATION_STARTED, context))
        builder.handle(LoopEvent(LoopEventType.ITERATION_COMPLETED, context))
        builder.handle(LoopEvent(LoopEventType.COMPLETION_EVALUATION_STARTED, context))
        builder.handle(LoopEvent(LoopEventType.LOOP_COMPLETED, context))
        pipeline.flush()

        spans = _spans(exporter)
        assert any(span.parent_span_id is None for span in spans)
        assert _scores(exporter) == []
        assert builder.resolve_parent_span_id("run-empty") is None
    finally:
        pipeline.shutdown()


def test_verification_without_score_produces_no_score_record() -> None:
    """验证结论缺少评分时不应产生 Score，但跨度照常关闭。"""
    exporter, pipeline, builder = _fixture()
    context = _context()
    try:
        _drive_iteration(builder, context, score=None)
        builder.handle(LoopEvent(LoopEventType.LOOP_COMPLETED, context))
        pipeline.flush()

        assert _scores(exporter) == []
        assert any(span.observation_type == "evaluator" for span in _spans(exporter))
    finally:
        pipeline.shutdown()


def test_concurrent_runs_keep_separate_traces() -> None:
    """交错推进的两个运行不应共享跨度状态。"""
    exporter, pipeline, builder = _fixture()
    first, second = _context("run-a"), _context("run-b")
    try:
        builder.handle(LoopEvent(LoopEventType.LOOP_STARTED, first))
        builder.handle(LoopEvent(LoopEventType.LOOP_STARTED, second))
        builder.handle(LoopEvent(LoopEventType.EXECUTION_DISPATCHED, first, detail="op-a"))
        builder.handle(LoopEvent(LoopEventType.EXECUTION_DISPATCHED, second, detail="op-b"))

        first_parent = builder.resolve_parent_span_id("run-a", "step-1")
        second_parent = builder.resolve_parent_span_id("run-b", "step-1")
        assert first_parent is not None
        assert second_parent is not None
        assert first_parent != second_parent

        builder.handle(LoopEvent(LoopEventType.LOOP_COMPLETED, first))
        assert builder.resolve_parent_span_id("run-a") is None
        assert builder.resolve_parent_span_id("run-b") is not None

        builder.handle(LoopEvent(LoopEventType.LOOP_COMPLETED, second))
        pipeline.flush()
        traces = {span.trace_id for span in _spans(exporter)}
        assert traces == {"run-a", "run-b"}
    finally:
        pipeline.shutdown()


def test_iteration_span_ids_are_unique() -> None:
    """每次迭代快照跨度都应使用独立标识。"""
    exporter, pipeline, builder = _fixture()
    context = _context()
    try:
        _drive_iteration(builder, context)
        context.current_step_index = 0
        _drive_iteration(builder, context)
        builder.handle(LoopEvent(LoopEventType.LOOP_COMPLETED, context))
        pipeline.flush()

        span_ids = [span.span_id for span in _spans(exporter)]
        assert len(span_ids) == len(set(span_ids))
        assert all(uuid4().hex != span_id for span_id in span_ids)
    finally:
        pipeline.shutdown()
