"""具有明确失败语义的组合事件发布器。"""

from __future__ import annotations

import logging
from enum import Enum

from matterloop_core import EventHandler, EventPublisher, LoopEvent

logger = logging.getLogger(__name__)


class PublisherFailureMode(str, Enum):
    """子发布器异常时的处理方式。"""

    RAISE = "raise"
    LOG_AND_CONTINUE = "log_and_continue"


class CompositeEventPublisher:
    """按顺序调用多个发布器并应用统一失败策略。"""

    def __init__(
        self,
        publishers: tuple[EventPublisher, ...],
        failure_mode: PublisherFailureMode = PublisherFailureMode.LOG_AND_CONTINUE,
    ) -> None:
        self._publishers = publishers
        self._failure_mode = failure_mode

    async def publish(self, event: LoopEvent) -> None:
        """向全部子发布器发布事件。"""
        for publisher in self._publishers:
            try:
                await publisher.publish(event)
            except Exception:
                if self._failure_mode is PublisherFailureMode.RAISE:
                    raise
                logger.exception(
                    "事件发布器执行失败",
                    extra={"run_id": event.context.run_id, "event": event.event_type.value},
                )


class HandlerEventPublisher:
    """把同步或异步 EventHandler 适配为 EventPublisher。"""

    def __init__(self, handler: EventHandler) -> None:
        self._handler = handler

    async def publish(self, event: LoopEvent) -> None:
        """调用处理器，并在需要时等待异步结果。"""
        import inspect

        result = self._handler(event)
        if inspect.isawaitable(result):
            await result
