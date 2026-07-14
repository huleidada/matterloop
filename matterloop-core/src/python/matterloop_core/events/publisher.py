"""零依赖的进程内生命周期事件发布器。"""

from __future__ import annotations

import inspect

from matterloop_core.events.models import EventHandler, LoopEvent


class LocalEventPublisher:
    """在进程内发布生命周期事件，不依赖外部基础设施。

    处理器按照注册顺序执行。内核需要确定性的调用顺序，因为审计持久化可能必须先于
    指标采集或通知操作完成。
    """

    def __init__(self) -> None:
        self._handlers: list[EventHandler] = []

    def subscribe(self, handler: EventHandler) -> None:
        """注册一个事件处理器，并避免重复注册。

        Args:
            handler: 后续事件触发时调用的同步或异步回调函数。
        """
        if handler not in self._handlers:
            self._handlers.append(handler)

    def unsubscribe(self, handler: EventHandler) -> None:
        """当指定处理器存在时将其移除。"""
        if handler in self._handlers:
            self._handlers.remove(handler)

    async def publish(self, event: LoopEvent) -> None:
        """将事件发布给当前处理器列表的稳定快照。"""
        for handler in tuple(self._handlers):
            result = handler(event)
            if inspect.isawaitable(result):
                await result
