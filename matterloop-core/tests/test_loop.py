"""最小 Loop 控制器的主要行为测试。"""

import asyncio

import pytest
from conftest import build_loop
from matterloop_core import (
    ExecutionResult,
    InvalidPlanError,
    LoopEvent,
    LoopEventType,
    LoopRequest,
    LoopStatus,
    Plan,
    PlanStep,
)


def test_loop_completes_and_emits_ordered_events() -> None:
    """验证通过的循环应持久化并发布完整且有序的生命周期事件。"""
    loop, store, events = build_loop()
    observed: list[LoopEventType] = []

    def collect(event: LoopEvent) -> None:
        observed.append(event.event_type)

    events.subscribe(collect)
    result = asyncio.run(loop.run(LoopRequest(goal="produce result")))

    assert result.status is LoopStatus.COMPLETED
    assert result.output == "produce result"
    assert result.cycles == 1
    assert result.total_attempts == 1
    assert result.completed_steps == 1
    assert store.contexts[result.run_id].status is LoopStatus.COMPLETED
    assert observed == [
        LoopEventType.LOOP_STARTED,
        LoopEventType.PLANNING_STARTED,
        LoopEventType.PLAN_CREATED,
        LoopEventType.EXECUTION_STARTED,
        LoopEventType.VERIFICATION_STARTED,
        LoopEventType.ITERATION_COMPLETED,
        LoopEventType.LOOP_COMPLETED,
    ]


def test_component_replacement_is_used_without_rebuilding_loop() -> None:
    """替换具名组件后，应在下一次查询时立即使用新组件。"""
    loop, _, _ = build_loop()

    class ReplacementExecutor:
        async def execute(self, step: PlanStep, context: object) -> ExecutionResult:
            del step, context
            return ExecutionResult("replacement")

    loop.executors.register("default", ReplacementExecutor(), replace=True)
    result = asyncio.run(loop.run(LoopRequest(goal="original")))

    assert result.output == "replacement"


def test_invalid_plan_marks_checkpoint_failed_and_reraises() -> None:
    """组件异常必须可观察、可持久化，并且不能被静默吞掉。"""
    loop, store, events = build_loop()
    observed: list[LoopEvent] = []
    events.subscribe(observed.append)

    class EmptyPlanner:
        async def plan(self, context: object) -> Plan:
            del context
            return Plan(())

    loop.planners.register("default", EmptyPlanner(), replace=True)

    with pytest.raises(InvalidPlanError):
        asyncio.run(loop.run(LoopRequest(goal="invalid")))

    failed_event = observed[-1]
    assert failed_event.event_type is LoopEventType.LOOP_FAILED
    assert failed_event.context.status is LoopStatus.FAILED
    assert store.contexts[failed_event.context.run_id].status is LoopStatus.FAILED
