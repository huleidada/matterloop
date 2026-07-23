"""生命周期处理器注册辅助与 Loop 之外的运行时信号总线。"""

from __future__ import annotations

import inspect
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from types import MappingProxyType

from matterloop_core import EventHandler, LoopEvent, LoopEventType

from matterloop_observability.bus import EventBus, Subscription


def on_task_created(bus: EventBus, handler: EventHandler) -> Subscription:
    """在任务创建（``LOOP_STARTED``）时调用处理器。"""
    return bus.subscribe(handler, event_types=(LoopEventType.LOOP_STARTED,))


def on_plan_created(bus: EventBus, handler: EventHandler) -> Subscription:
    """在计划生成完成（``PLAN_CREATED``）时调用处理器。"""
    return bus.subscribe(handler, event_types=(LoopEventType.PLAN_CREATED,))


def on_execution_completed(bus: EventBus, handler: EventHandler) -> Subscription:
    """在步骤执行完成（``EXECUTION_COMPLETED``）时调用处理器。"""
    return bus.subscribe(handler, event_types=(LoopEventType.EXECUTION_COMPLETED,))


def on_verification_failed(bus: EventBus, handler: EventHandler) -> Subscription:
    """在验证失败的迭代完成时调用处理器。

    core 未提供独立的验证失败事件：验证结论随 ``ITERATION_COMPLETED`` 事件
    写入最近一条迭代记录，这里订阅该事件并按记录中的验证结论过滤。
    """
    return bus.subscribe(
        handler,
        event_types=(LoopEventType.ITERATION_COMPLETED,),
        predicate=_last_verification_failed,
    )


def on_human_interaction_requested(bus: EventBus, handler: EventHandler) -> Subscription:
    """在请求人工交互（``HUMAN_INTERACTION_REQUESTED``）时调用处理器。"""
    return bus.subscribe(handler, event_types=(LoopEventType.HUMAN_INTERACTION_REQUESTED,))


def _last_verification_failed(event: LoopEvent) -> bool:
    """判断事件上下文中最近一次验证是否失败。"""
    records = event.context.records
    return bool(records) and not records[-1].verification.passed


@dataclass(frozen=True, slots=True)
class RuntimeSignal:
    """Loop 生命周期之外的自定义运行时信号。

    Args:
        name: 信号名称，例如 ``SignalBus.MEMORY_UPDATED``。
        payload: 只读的信号负载。
        created_at: 信号产生时间。
    """

    name: str
    payload: Mapping[str, object] = field(default_factory=dict)
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def __post_init__(self) -> None:
        """校验名称并冻结负载。"""
        if not self.name.strip():
            raise ValueError("name must not be empty")
        object.__setattr__(self, "payload", MappingProxyType(dict(self.payload)))


SignalHandler = Callable[[RuntimeSignal], Awaitable[None] | None]


class SignalSubscription:
    """由 :class:`SignalBus` 创建的可取消信号订阅句柄。"""

    def __init__(self, bus: SignalBus, name: str, handler: SignalHandler) -> None:
        self._bus = bus
        self._name = name
        self._handler = handler

    async def deliver(self, signal: RuntimeSignal) -> None:
        """调用处理器，并在需要时等待异步结果。"""
        result = self._handler(signal)
        if inspect.isawaitable(result):
            await result

    def cancel(self) -> None:
        """取消订阅；重复取消是安全的空操作。"""
        self._bus._discard(self._name, self)


class SignalBus:
    """按信号名称分发 :class:`RuntimeSignal` 的轻量发布/订阅总线。"""

    MEMORY_UPDATED = "memory_updated"
    COST_RECORDED = "cost_recorded"

    def __init__(self) -> None:
        self._subscriptions: dict[str, list[SignalSubscription]] = {}

    def subscribe(self, name: str, handler: SignalHandler) -> SignalSubscription:
        """为指定信号名称注册一个同步或异步处理器。

        Args:
            name: 目标信号名称。
            handler: 信号到达时调用的回调函数。

        Returns:
            可通过 ``cancel()`` 取消的订阅句柄。
        """
        subscription = SignalSubscription(self, name, handler)
        self._subscriptions.setdefault(name, []).append(subscription)
        return subscription

    async def publish(self, signal: RuntimeSignal) -> None:
        """把信号按注册顺序分发给同名订阅者。"""
        for subscription in tuple(self._subscriptions.get(signal.name, ())):
            await subscription.deliver(signal)

    def _discard(self, name: str, subscription: SignalSubscription) -> None:
        """移除一个订阅；订阅不存在时静默忽略。"""
        registered = self._subscriptions.get(name, [])
        if subscription in registered:
            registered.remove(subscription)
