"""事件路由器的优先级、多命中与失败模式测试。"""

import logging

import pytest
from matterloop_core import LoopContext, LoopEvent, LoopEventType, LoopRequest
from matterloop_observability import EventBus, EventRouter, EventRule, PublisherFailureMode


def _event(
    event_type: LoopEventType = LoopEventType.EXECUTION_COMPLETED,
    run_id: str = "run-1",
) -> LoopEvent:
    """创建不依赖运行时的测试事件。"""
    context = LoopContext(LoopRequest("验证事件路由"), run_id=run_id)
    return LoopEvent(event_type, context)


async def test_route_matches_by_priority_and_reports_hits() -> None:
    """多条命中规则必须按优先级执行并返回命中名单。"""
    router = EventRouter()
    executed: list[str] = []

    router.add_rule(
        EventRule(
            name="second",
            action=lambda event: executed.append("second"),
            priority=20,
        )
    )
    router.add_rule(
        EventRule(
            name="first",
            action=lambda event: executed.append("first"),
            event_types=(LoopEventType.EXECUTION_COMPLETED,),
            priority=10,
        )
    )
    router.add_rule(
        EventRule(
            name="unmatched",
            action=lambda event: executed.append("unmatched"),
            event_types=(LoopEventType.LOOP_FAILED,),
            priority=1,
        )
    )

    matched = await router.route(_event())

    assert matched == ("first", "second")
    assert executed == ["first", "second"]


async def test_rule_predicate_filters_events() -> None:
    """规则谓词不满足时动作不得执行。"""
    router = EventRouter()
    executed: list[str] = []

    router.add_rule(
        EventRule(
            name="tenant-only",
            action=lambda event: executed.append(event.context.run_id),
            predicate=lambda event: event.context.run_id == "run-match",
        )
    )

    assert await router.route(_event(run_id="run-other")) == ()
    assert await router.route(_event(run_id="run-match")) == ("tenant-only",)
    assert executed == ["run-match"]


async def test_add_and_remove_rule() -> None:
    """同名规则必须拒绝注册；移除后不再命中。"""
    router = EventRouter()
    rule = EventRule(name="audit", action=lambda event: None)
    router.add_rule(rule)

    with pytest.raises(ValueError, match="already registered"):
        router.add_rule(EventRule(name="audit", action=lambda event: None))

    assert router.remove_rule("audit") is True
    assert router.remove_rule("audit") is False
    assert await router.route(_event()) == ()


async def test_log_and_continue_keeps_matching(caplog: pytest.LogCaptureFixture) -> None:
    """容错模式下失败动作仍计入命中且不阻断后续规则。"""
    router = EventRouter(failure_mode=PublisherFailureMode.LOG_AND_CONTINUE)
    executed: list[str] = []

    def fail(event: LoopEvent) -> None:
        del event
        raise RuntimeError("action failed")

    router.add_rule(EventRule(name="broken", action=fail, priority=1))
    router.add_rule(EventRule(name="next", action=lambda event: executed.append("next")))

    with caplog.at_level(logging.ERROR):
        matched = await router.route(_event())

    assert matched == ("broken", "next")
    assert executed == ["next"]
    assert "事件路由动作执行失败" in caplog.text


async def test_raise_mode_propagates_action_error() -> None:
    """严格模式必须把动作异常传播给调用方。"""
    router = EventRouter(failure_mode=PublisherFailureMode.RAISE)

    def fail(event: LoopEvent) -> None:
        del event
        raise RuntimeError("action failed")

    router.add_rule(EventRule(name="broken", action=fail))

    with pytest.raises(RuntimeError, match="action failed"):
        await router.route(_event())


async def test_attach_to_bus_triggers_next_stage_action() -> None:
    """路由器挂到总线后，事件应自动触发下一阶段动作。"""
    bus = EventBus()
    router = EventRouter()
    triggered: list[str] = []

    async def start_next_stage(event: LoopEvent) -> None:
        triggered.append(f"next-stage:{event.context.run_id}")

    router.add_rule(
        EventRule(
            name="execution-to-next-stage",
            action=start_next_stage,
            event_types=(LoopEventType.EXECUTION_COMPLETED,),
        )
    )
    subscription = router.attach_to(bus)

    await bus.publish(_event(LoopEventType.EXECUTION_COMPLETED))
    await bus.publish(_event(LoopEventType.LOOP_STARTED))

    assert triggered == ["next-stage:run-1"]

    subscription.cancel()
    await bus.publish(_event(LoopEventType.EXECUTION_COMPLETED))
    assert triggered == ["next-stage:run-1"]
