"""基于规则的事件路由器，用于按事件自动触发后续动作。"""

from __future__ import annotations

import inspect
import logging
from dataclasses import dataclass

from matterloop_core import EventHandler, LoopEvent, LoopEventType

from matterloop_observability.bus import EventBus, EventPredicate, Subscription
from matterloop_observability.publisher import PublisherFailureMode

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class EventRule:
    """描述一条不可变的事件路由规则。

    Args:
        name: 规则的唯一名称，用于审计与移除。
        action: 命中后执行的同步或异步动作。
        event_types: 参与匹配的事件类型；空元组表示匹配全部类型。
        predicate: 可选的附加匹配谓词。
        priority: 执行优先级，数值越小越先执行。
    """

    name: str
    action: EventHandler
    event_types: tuple[LoopEventType, ...] = ()
    predicate: EventPredicate | None = None
    priority: int = 100

    def __post_init__(self) -> None:
        """拒绝无法在审计结果中定位的规则。"""
        if not self.name.strip():
            raise ValueError("name must not be empty")

    def matches(self, event: LoopEvent) -> bool:
        """判断事件是否同时满足类型与谓词条件。"""
        if self.event_types and event.event_type not in self.event_types:
            return False
        return self.predicate is None or self.predicate(event)


class EventRouter:
    """按优先级匹配规则并执行动作，支持一个事件命中多条规则。"""

    def __init__(
        self,
        failure_mode: PublisherFailureMode = PublisherFailureMode.LOG_AND_CONTINUE,
    ) -> None:
        self._rules: list[EventRule] = []
        self._failure_mode = failure_mode

    def add_rule(self, rule: EventRule) -> None:
        """注册一条规则。

        Args:
            rule: 待注册的路由规则。

        Raises:
            ValueError: 当同名规则已存在时抛出。
        """
        if any(existing.name == rule.name for existing in self._rules):
            raise ValueError(f"rule {rule.name!r} already registered")
        self._rules.append(rule)

    def remove_rule(self, name: str) -> bool:
        """移除指定名称的规则。

        Args:
            name: 待移除规则的名称。

        Returns:
            规则存在并被移除时返回 ``True``。
        """
        for index, rule in enumerate(self._rules):
            if rule.name == name:
                del self._rules[index]
                return True
        return False

    async def route(self, event: LoopEvent) -> tuple[str, ...]:
        """按优先级执行全部命中规则并返回其名称。

        优先级相同的规则保持注册顺序（稳定排序）。``LOG_AND_CONTINUE``
        模式下动作失败仍计入命中名单，便于审计实际触发过的规则。

        Args:
            event: 待路由的生命周期事件。

        Returns:
            按执行顺序排列的命中规则名称。
        """
        matched: list[str] = []
        for rule in sorted(self._rules, key=lambda item: item.priority):
            if not rule.matches(event):
                continue
            matched.append(rule.name)
            try:
                result = rule.action(event)
                if inspect.isawaitable(result):
                    await result
            except Exception:
                if self._failure_mode is PublisherFailureMode.RAISE:
                    raise
                logger.exception(
                    "事件路由动作执行失败",
                    extra={
                        "rule": rule.name,
                        "run_id": event.context.run_id,
                        "event": event.event_type.value,
                    },
                )
        return tuple(matched)

    def attach_to(self, bus: EventBus) -> Subscription:
        """把路由器作为订阅者挂载到事件总线。

        Args:
            bus: 目标事件总线。

        Returns:
            可取消的订阅句柄。
        """

        async def _dispatch(event: LoopEvent) -> None:
            await self.route(event)

        return bus.subscribe(_dispatch)
