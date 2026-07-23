"""生命周期注册辅助与运行时信号总线测试。"""

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
    EventBus,
    RuntimeSignal,
    SignalBus,
    on_execution_completed,
    on_human_interaction_requested,
    on_plan_created,
    on_task_created,
    on_verification_failed,
)


def _event(event_type: LoopEventType, verification_passed: bool | None = None) -> LoopEvent:
    """创建测试事件，可附带一条带验证结论的迭代记录。"""
    context = LoopContext(LoopRequest("验证生命周期辅助"), run_id="run-1")
    if verification_passed is not None:
        context.records.append(
            IterationRecord(
                cycle=1,
                step_index=0,
                step=PlanStep("执行一个步骤"),
                execution=ExecutionResult("完成"),
                verification=VerificationResult(
                    passed=verification_passed,
                    feedback="" if verification_passed else "验收未通过",
                ),
            )
        )
    return LoopEvent(event_type, context)


async def test_lifecycle_helpers_bind_expected_event_types() -> None:
    """各注册辅助必须只响应对应的生命周期事件。"""
    bus = EventBus()
    received: list[str] = []

    on_task_created(bus, lambda event: received.append("task_created"))
    on_plan_created(bus, lambda event: received.append("plan_created"))
    on_execution_completed(bus, lambda event: received.append("execution_completed"))
    on_human_interaction_requested(bus, lambda event: received.append("human_interaction"))

    await bus.publish(_event(LoopEventType.LOOP_STARTED))
    await bus.publish(_event(LoopEventType.PLAN_CREATED))
    await bus.publish(_event(LoopEventType.EXECUTION_COMPLETED))
    await bus.publish(_event(LoopEventType.HUMAN_INTERACTION_REQUESTED))
    await bus.publish(_event(LoopEventType.LOOP_COMPLETED))

    assert received == [
        "task_created",
        "plan_created",
        "execution_completed",
        "human_interaction",
    ]


async def test_on_verification_failed_filters_by_verification_outcome() -> None:
    """只有最近一次验证失败的迭代完成事件才触发处理器。"""
    bus = EventBus()
    received: list[str] = []

    async def collect(event: LoopEvent) -> None:
        received.append(event.context.records[-1].verification.feedback)

    subscription = on_verification_failed(bus, collect)

    await bus.publish(_event(LoopEventType.ITERATION_COMPLETED, verification_passed=True))
    await bus.publish(_event(LoopEventType.ITERATION_COMPLETED))
    await bus.publish(_event(LoopEventType.ITERATION_COMPLETED, verification_passed=False))

    assert received == ["验收未通过"]

    subscription.cancel()
    await bus.publish(_event(LoopEventType.ITERATION_COMPLETED, verification_passed=False))
    assert received == ["验收未通过"]


def test_runtime_signal_freezes_payload_and_validates_name() -> None:
    """信号负载必须只读，空名称必须被拒绝。"""
    signal = RuntimeSignal(SignalBus.MEMORY_UPDATED, payload={"key": "value"})

    assert signal.payload == {"key": "value"}
    with pytest.raises(TypeError):
        signal.payload["key"] = "mutated"  # type: ignore[index]
    with pytest.raises(ValueError, match="name"):
        RuntimeSignal("  ")


async def test_signal_bus_dispatches_sync_and_async_handlers() -> None:
    """同步与异步处理器都应按名称收到信号；取消后停止接收。"""
    bus = SignalBus()
    received: list[str] = []

    def sync_handler(signal: RuntimeSignal) -> None:
        received.append(f"sync:{signal.name}")

    async def async_handler(signal: RuntimeSignal) -> None:
        received.append(f"async:{signal.payload['entry']}")

    bus.subscribe(SignalBus.MEMORY_UPDATED, sync_handler)
    subscription = bus.subscribe(SignalBus.MEMORY_UPDATED, async_handler)
    bus.subscribe(SignalBus.COST_RECORDED, lambda signal: received.append("cost"))

    await bus.publish(RuntimeSignal(SignalBus.MEMORY_UPDATED, payload={"entry": "note-1"}))

    assert received == ["sync:memory_updated", "async:note-1"]

    subscription.cancel()
    await bus.publish(RuntimeSignal(SignalBus.MEMORY_UPDATED, payload={"entry": "note-2"}))
    assert received == ["sync:memory_updated", "async:note-1", "sync:memory_updated"]
