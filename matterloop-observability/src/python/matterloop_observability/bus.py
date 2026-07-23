"""进程内事件总线：订阅过滤、可选持久化与统一失败策略。"""

from __future__ import annotations

import inspect
import logging
from collections.abc import Callable, Iterable
from typing import Protocol, runtime_checkable

from matterloop_core import EventHandler, LoopEvent, LoopEventType

from matterloop_observability.publisher import PublisherFailureMode

logger = logging.getLogger(__name__)

EventPredicate = Callable[[LoopEvent], bool]


@runtime_checkable
class EventStore(Protocol):
    """持久化生命周期事件的最小存储协议。"""

    async def append(self, event: LoopEvent) -> None:
        """追加一个生命周期事件。

        Args:
            event: 待持久化的不可变事件。
        """
        ...

    async def list_events(self, run_id: str, limit: int | None = None) -> tuple[LoopEvent, ...]:
        """按事件序号升序返回指定运行的事件。

        Args:
            run_id: 目标运行标识。
            limit: 可选的最大返回数量。

        Returns:
            按 ``sequence`` 升序排列的事件元组。
        """
        ...


class InMemoryEventStore:
    """按 run_id 分组保存事件的进程内存储，适合测试与单进程运行。"""

    def __init__(self) -> None:
        self._events: dict[str, list[LoopEvent]] = {}

    async def append(self, event: LoopEvent) -> None:
        """把事件追加到所属运行的分组。"""
        self._events.setdefault(event.context.run_id, []).append(event)

    async def list_events(self, run_id: str, limit: int | None = None) -> tuple[LoopEvent, ...]:
        """按 ``sequence`` 升序返回事件，可选截断数量。"""
        events = sorted(self._events.get(run_id, ()), key=lambda item: item.sequence)
        if limit is not None:
            events = events[:limit]
        return tuple(events)


class Subscription:
    """由 :class:`EventBus` 创建的可取消订阅句柄。"""

    def __init__(
        self,
        bus: EventBus,
        handler: EventHandler,
        event_types: frozenset[LoopEventType] | None,
        predicate: EventPredicate | None,
    ) -> None:
        self._bus = bus
        self._handler = handler
        self._event_types = event_types
        self._predicate = predicate

    def matches(self, event: LoopEvent) -> bool:
        """判断事件是否命中本订阅的类型与谓词过滤条件。"""
        if self._event_types is not None and event.event_type not in self._event_types:
            return False
        return self._predicate is None or self._predicate(event)

    async def deliver(self, event: LoopEvent) -> None:
        """调用处理器，并在需要时等待异步结果。"""
        result = self._handler(event)
        if inspect.isawaitable(result):
            await result

    def cancel(self) -> None:
        """取消订阅；重复取消是安全的空操作。"""
        self._bus._discard(self)


class EventBus:
    """实现 core ``EventPublisher`` 协议的进程内发布/订阅总线。

    发布顺序：先把事件写入可选的 :class:`EventStore`，再按订阅注册顺序分发
    给匹配的订阅者，保证审计持久化先于业务处理器执行。失败策略与
    :class:`~matterloop_observability.publisher.CompositeEventPublisher` 保持
    一致，由 :class:`PublisherFailureMode` 控制。
    """

    def __init__(
        self,
        store: EventStore | None = None,
        failure_mode: PublisherFailureMode = PublisherFailureMode.LOG_AND_CONTINUE,
    ) -> None:
        self._store = store
        self._failure_mode = failure_mode
        self._subscriptions: list[Subscription] = []

    def subscribe(
        self,
        handler: EventHandler,
        *,
        event_types: Iterable[LoopEventType] | None = None,
        predicate: EventPredicate | None = None,
    ) -> Subscription:
        """注册一个同步或异步事件处理器。

        Args:
            handler: 命中过滤条件时调用的回调函数。
            event_types: 参与匹配的事件类型；``None`` 或空集合表示全部类型。
            predicate: 可选的附加匹配谓词。

        Returns:
            可通过 ``cancel()`` 取消的订阅句柄。
        """
        types = frozenset(event_types) if event_types else None
        subscription = Subscription(self, handler, types, predicate)
        self._subscriptions.append(subscription)
        return subscription

    async def publish(self, event: LoopEvent) -> None:
        """先持久化事件，再按注册顺序分发给匹配的订阅者。"""
        if self._store is not None:
            try:
                await self._store.append(event)
            except Exception:
                self._handle_failure(event, stage="持久化")
        for subscription in tuple(self._subscriptions):
            if not subscription.matches(event):
                continue
            try:
                await subscription.deliver(event)
            except Exception:
                self._handle_failure(event, stage="订阅者")

    def _discard(self, subscription: Subscription) -> None:
        """移除一个订阅；订阅不存在时静默忽略。"""
        if subscription in self._subscriptions:
            self._subscriptions.remove(subscription)

    def _handle_failure(self, event: LoopEvent, stage: str) -> None:
        """按失败模式传播或记录当前异常。"""
        if self._failure_mode is PublisherFailureMode.RAISE:
            raise
        logger.exception(
            "事件总线%s执行失败",
            stage,
            extra={"run_id": event.context.run_id, "event": event.event_type.value},
        )
