"""事件总线的订阅过滤、失败模式与持久化测试。"""

import logging

import pytest
from matterloop_core import LoopContext, LoopEvent, LoopEventType, LoopRequest
from matterloop_observability import EventBus, InMemoryEventStore, PublisherFailureMode


def _event(
    event_type: LoopEventType = LoopEventType.LOOP_STARTED,
    run_id: str = "run-1",
    sequence: int = 0,
) -> LoopEvent:
    """创建不依赖运行时的测试事件。"""
    context = LoopContext(LoopRequest("验证事件总线"), run_id=run_id)
    return LoopEvent(event_type, context, sequence=sequence)


async def test_subscribe_filters_by_event_type() -> None:
    """只订阅指定类型时其他事件不得触达处理器。"""
    bus = EventBus()
    received: list[str] = []

    bus.subscribe(
        lambda event: received.append(event.event_type.value),
        event_types=(LoopEventType.PLAN_CREATED,),
    )

    await bus.publish(_event(LoopEventType.LOOP_STARTED))
    await bus.publish(_event(LoopEventType.PLAN_CREATED))

    assert received == [LoopEventType.PLAN_CREATED.value]


async def test_subscribe_filters_by_predicate() -> None:
    """谓词过滤必须在类型过滤之外独立生效。"""
    bus = EventBus()
    received: list[str] = []

    bus.subscribe(
        lambda event: received.append(event.context.run_id),
        predicate=lambda event: event.context.run_id == "run-match",
    )

    await bus.publish(_event(run_id="run-other"))
    await bus.publish(_event(run_id="run-match"))

    assert received == ["run-match"]


async def test_async_handler_is_awaited_and_subscription_cancellable() -> None:
    """异步处理器应被等待完成；取消后不再接收事件。"""
    bus = EventBus()
    received: list[str] = []

    async def collect(event: LoopEvent) -> None:
        received.append(event.event_type.value)

    subscription = bus.subscribe(collect)

    await bus.publish(_event())
    subscription.cancel()
    await bus.publish(_event())

    assert received == [LoopEventType.LOOP_STARTED.value]


async def test_log_and_continue_mode_keeps_dispatching(caplog: pytest.LogCaptureFixture) -> None:
    """容错模式下失败订阅者不得阻断后续订阅者。"""
    bus = EventBus(failure_mode=PublisherFailureMode.LOG_AND_CONTINUE)
    received: list[str] = []

    def fail(event: LoopEvent) -> None:
        del event
        raise RuntimeError("handler failed")

    bus.subscribe(fail)
    bus.subscribe(lambda event: received.append(event.event_type.value))

    with caplog.at_level(logging.ERROR):
        await bus.publish(_event())

    assert received == [LoopEventType.LOOP_STARTED.value]
    assert "事件总线订阅者执行失败" in caplog.text


async def test_raise_mode_propagates_handler_error() -> None:
    """严格模式必须把订阅者异常传播给调用方。"""
    bus = EventBus(failure_mode=PublisherFailureMode.RAISE)

    def fail(event: LoopEvent) -> None:
        del event
        raise RuntimeError("handler failed")

    bus.subscribe(fail)

    with pytest.raises(RuntimeError, match="handler failed"):
        await bus.publish(_event())


async def test_store_persists_before_dispatch() -> None:
    """事件必须先写入存储，再分发给订阅者。"""
    store = InMemoryEventStore()
    bus = EventBus(store=store)
    persisted_at_dispatch: list[int] = []

    async def observe(event: LoopEvent) -> None:
        del event
        persisted_at_dispatch.append(len(await store.list_events("run-1")))

    bus.subscribe(observe)

    await bus.publish(_event(sequence=1))

    assert persisted_at_dispatch == [1]


async def test_store_orders_by_sequence_and_supports_limit() -> None:
    """存储按 sequence 升序返回，并支持数量截断和运行隔离。"""
    store = InMemoryEventStore()
    bus = EventBus(store=store)

    await bus.publish(_event(LoopEventType.PLAN_CREATED, sequence=2))
    await bus.publish(_event(LoopEventType.LOOP_STARTED, sequence=1))
    await bus.publish(_event(LoopEventType.LOOP_COMPLETED, sequence=3))
    await bus.publish(_event(run_id="run-2", sequence=1))

    events = await store.list_events("run-1")
    assert [event.sequence for event in events] == [1, 2, 3]
    assert [event.event_type for event in events] == [
        LoopEventType.LOOP_STARTED,
        LoopEventType.PLAN_CREATED,
        LoopEventType.LOOP_COMPLETED,
    ]

    limited = await store.list_events("run-1", limit=2)
    assert [event.sequence for event in limited] == [1, 2]
    assert await store.list_events("run-absent") == ()
