"""Loop 生命周期事件模型。"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum

from matterloop_core.context import LoopContext


class LoopEventType(str, Enum):
    """提供给集成模块使用的稳定生命周期事件名称。"""

    LOOP_STARTED = "loop.started"
    LOOP_RESUMED = "loop.resumed"
    PLANNING_STARTED = "planning.started"
    PLAN_CREATED = "plan.created"
    APPROVAL_REQUESTED = "approval.requested"
    APPROVAL_GRANTED = "approval.granted"
    HUMAN_INTERACTION_REQUESTED = "human.interaction_requested"
    HUMAN_RESPONSE_SUBMITTED = "human.response_submitted"
    HUMAN_APPROVED = "human.approved"
    HUMAN_REJECTED = "human.rejected"
    HUMAN_REVISED = "human.revised"
    HUMAN_INPUT_PROVIDED = "human.input_provided"
    LOOP_PAUSED = "loop.paused"
    COMPONENT_RETRYING = "component.retrying"
    EXECUTION_STARTED = "execution.started"
    VERIFICATION_STARTED = "verification.started"
    ITERATION_COMPLETED = "iteration.completed"
    COMPLETION_EVALUATION_STARTED = "completion.evaluation_started"
    COMPLETION_REPLAN_REQUESTED = "completion.replan_requested"
    LOOP_COMPLETED = "loop.completed"
    LOOP_BLOCKED = "loop.blocked"
    LOOP_CANCELLED = "loop.cancelled"
    LOOP_TIMED_OUT = "loop.timed_out"
    LOOP_FAILED = "loop.failed"


@dataclass(frozen=True, slots=True)
class LoopEvent:
    """向生命周期订阅者传递隔离后的上下文快照。"""

    event_type: LoopEventType
    context: LoopContext
    occurred_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    detail: str = ""
    sequence: int = 0


EventHandler = Callable[[LoopEvent], Awaitable[None] | None]
