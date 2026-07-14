"""团队协作生命周期事件与本地发布器。"""

from __future__ import annotations

import inspect
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from threading import RLock

from matterloop_agents.collaboration._immutability import freeze_mapping
from matterloop_agents.collaboration.models import TeamSnapshot


class TeamEventType(str, Enum):
    """团队协作过程中的稳定生命周期事件。"""

    TEAM_STARTED = "team.started"
    PLANNING_STARTED = "planning.started"
    PLAN_CREATED = "plan.created"
    REPLAN_REQUESTED = "plan.replan_requested"
    TASK_READY = "task.ready"
    TASK_ASSIGNED = "task.assigned"
    TASK_STARTED = "task.started"
    TASK_VERIFYING = "task.verifying"
    TASK_VERIFIED = "task.verified"
    TASK_RETRYING = "task.retrying"
    TASK_COMPLETED = "task.completed"
    TASK_FAILED = "task.failed"
    APPROVAL_REQUESTED = "approval.requested"
    APPROVAL_GRANTED = "approval.granted"
    APPROVAL_REJECTED = "approval.rejected"
    REVIEW_STARTED = "review.started"
    REVIEW_COMPLETED = "review.completed"
    HUMAN_INTERACTION_REQUESTED = "human.interaction_requested"
    HUMAN_RESPONSE_SUBMITTED = "human.response_submitted"
    HUMAN_APPROVED = "human.approved"
    HUMAN_REJECTED = "human.rejected"
    HUMAN_REVISED = "human.revised"
    HUMAN_INPUT_PROVIDED = "human.input_provided"
    TEAM_PAUSED = "team.paused"
    TEAM_RESUMED = "team.resumed"
    TEAM_COMPLETED = "team.completed"
    TEAM_BLOCKED = "team.blocked"
    TEAM_CANCELLED = "team.cancelled"
    TEAM_TIMED_OUT = "team.timed_out"
    TEAM_FAILED = "team.failed"


@dataclass(frozen=True, slots=True)
class TeamEvent:
    """向观察者传递不可变团队快照的生命周期事件。

    Args:
        event_type: 稳定事件类型。
        snapshot: 事件发生时的团队快照。
        detail: 面向诊断的非敏感简短说明。
        metadata: 只读扩展信息。
        occurred_at: 带时区的事件发生时间。
    """

    event_type: TeamEventType
    snapshot: TeamSnapshot
    detail: str = ""
    metadata: Mapping[str, object] = field(default_factory=dict)
    occurred_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def __post_init__(self) -> None:
        """冻结扩展信息并拒绝无时区时间。"""
        if self.occurred_at.tzinfo is None:
            raise ValueError("occurred_at must include a timezone")
        object.__setattr__(self, "metadata", freeze_mapping(self.metadata))


TeamEventHandler = Callable[[TeamEvent], Awaitable[None] | None]


class LocalTeamEventPublisher:
    """按照订阅顺序发布事件的线程安全进程内发布器。"""

    def __init__(self) -> None:
        self._handlers: list[TeamEventHandler] = []
        self._lock = RLock()

    def subscribe(self, handler: TeamEventHandler) -> None:
        """注册处理器，并避免同一对象重复注册。

        Args:
            handler: 同步或异步事件回调。
        """
        with self._lock:
            if handler not in self._handlers:
                self._handlers.append(handler)

    def unsubscribe(self, handler: TeamEventHandler) -> None:
        """移除已经注册的处理器；不存在时保持幂等。

        Args:
            handler: 不再接收事件的回调。
        """
        with self._lock:
            if handler in self._handlers:
                self._handlers.remove(handler)

    async def publish(self, event: TeamEvent) -> None:
        """向订阅时序快照中的处理器发布事件。

        Args:
            event: 等待发布的不可变团队事件。
        """
        with self._lock:
            handlers = tuple(self._handlers)
        for handler in handlers:
            result = handler(event)
            if inspect.isawaitable(result):
                await result


__all__ = ["LocalTeamEventPublisher", "TeamEvent", "TeamEventHandler", "TeamEventType"]
