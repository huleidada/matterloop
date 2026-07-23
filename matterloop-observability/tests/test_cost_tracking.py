"""成本记录、聚合与事件驱动成本采集测试。"""

from collections.abc import Mapping

import pytest
from matterloop_core import (
    ExecutionResult,
    IterationRecord,
    LoopContext,
    LoopEvent,
    LoopEventType,
    LoopRequest,
    PlanStep,
    VerificationResult,
)
from matterloop_observability import (
    CostRecord,
    CostSummary,
    CostTracker,
    CostTrackingHandler,
    EventBus,
)


def _record(
    run_id: str = "run-1",
    tenant_id: str | None = None,
    tokens_input: int = 10,
    tokens_output: int = 5,
    cost_micro_units: int = 100,
    tool_calls: int = 1,
) -> CostRecord:
    """创建一条测试成本记录。"""
    return CostRecord(
        run_id=run_id,
        tenant_id=tenant_id,
        tokens_input=tokens_input,
        tokens_output=tokens_output,
        cost_micro_units=cost_micro_units,
        tool_calls=tool_calls,
    )


def _execution_event(
    run_id: str = "run-1",
    execution_metadata: Mapping[str, object] | None = None,
    request_metadata: Mapping[str, object] | None = None,
) -> LoopEvent:
    """创建一个带迭代记录的执行完成事件。"""
    context = LoopContext(
        LoopRequest("验证成本追踪", metadata=request_metadata or {}), run_id=run_id
    )
    context.records.append(
        IterationRecord(
            cycle=1,
            step_index=0,
            step=PlanStep("执行一个步骤"),
            execution=ExecutionResult("完成", metadata=execution_metadata or {}),
            verification=VerificationResult(passed=True),
        )
    )
    return LoopEvent(LoopEventType.EXECUTION_COMPLETED, context)


def test_cost_record_rejects_invalid_values() -> None:
    """空 run_id 与负用量必须被拒绝。"""
    with pytest.raises(ValueError, match="run_id"):
        _record(run_id="  ")
    with pytest.raises(ValueError, match="negative"):
        _record(tokens_input=-1)


def test_tracker_aggregates_by_run_tenant_and_overall() -> None:
    """按运行、按租户与全局聚合必须互相一致。"""
    tracker = CostTracker()
    tracker.record(_record(run_id="run-1", tenant_id="tenant-a"))
    tracker.record(_record(run_id="run-1", tenant_id="tenant-a", tokens_input=20, tool_calls=2))
    tracker.record(_record(run_id="run-2", tenant_id="tenant-b", cost_micro_units=900))

    assert tracker.total_for_run("run-1") == CostSummary(
        records=2,
        tokens_input=30,
        tokens_output=10,
        cost_micro_units=200,
        tool_calls=3,
    )
    assert tracker.total_for_tenant("tenant-b") == CostSummary(
        records=1,
        tokens_input=10,
        tokens_output=5,
        cost_micro_units=900,
        tool_calls=1,
    )
    assert tracker.total_for_run("run-absent") == CostSummary()
    assert tracker.summary() == CostSummary(
        records=3,
        tokens_input=40,
        tokens_output=15,
        cost_micro_units=1100,
        tool_calls=4,
    )


async def test_handler_extracts_usage_from_execution_metadata() -> None:
    """处理器应优先读取最近一条执行结果的用量字段。"""
    tracker = CostTracker()
    bus = EventBus()
    bus.subscribe(
        CostTrackingHandler(tracker),
        event_types=(LoopEventType.EXECUTION_COMPLETED,),
    )

    await bus.publish(
        _execution_event(
            execution_metadata={
                "tokens_input": 120,
                "tokens_output": 30,
                "cost_micro_units": 4500,
                "tool_calls": 2,
                "tenant_id": "tenant-a",
            }
        )
    )

    assert tracker.total_for_run("run-1") == CostSummary(
        records=1,
        tokens_input=120,
        tokens_output=30,
        cost_micro_units=4500,
        tool_calls=2,
    )
    assert tracker.total_for_tenant("tenant-a").records == 1


async def test_handler_falls_back_to_request_metadata() -> None:
    """执行结果没有用量键时应回退到请求 metadata。"""
    tracker = CostTracker()
    handler = CostTrackingHandler(tracker)

    handler(
        _execution_event(
            request_metadata={"tokens_input": 7, "cost_micro_units": 90, "tenant_id": "tenant-b"}
        )
    )

    assert tracker.total_for_tenant("tenant-b") == CostSummary(
        records=1,
        tokens_input=7,
        tokens_output=0,
        cost_micro_units=90,
        tool_calls=0,
    )


async def test_handler_skips_events_without_usage_metadata() -> None:
    """缺少全部用量键的事件必须被跳过且不报错。"""
    tracker = CostTracker()
    handler = CostTrackingHandler(tracker)

    handler(_execution_event(execution_metadata={"note": "无用量"}))
    handler(
        LoopEvent(
            LoopEventType.LOOP_STARTED,
            LoopContext(LoopRequest("无用量事件"), run_id="run-1"),
        )
    )

    assert tracker.summary() == CostSummary()
