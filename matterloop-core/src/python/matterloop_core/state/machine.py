"""Loop 状态定义与受保护的状态转换规则。"""

from enum import Enum

from matterloop_core.exceptions import InvalidStateTransitionError


class LoopStatus(str, Enum):
    """一次 Loop 运行包含的生命周期状态。"""

    CREATED = "created"
    PLANNING = "planning"
    EXECUTING = "executing"
    VERIFYING = "verifying"
    WAITING_APPROVAL = "waiting_approval"
    PAUSED = "paused"
    COMPLETED = "completed"
    BLOCKED = "blocked"
    CANCELLED = "cancelled"
    TIMED_OUT = "timed_out"
    FAILED = "failed"

    @property
    def is_terminal(self) -> bool:
        """判断当前状态是否为不允许继续转换的终态。"""
        return self in {self.COMPLETED, self.CANCELLED, self.TIMED_OUT, self.FAILED}


class StopReason(str, Enum):
    """Loop 停止运行的结构化原因。"""

    COMPLETED = "completed"
    POLICY_REJECTED = "policy_rejected"
    APPROVAL_REJECTED = "approval_rejected"
    APPROVAL_DEFERRED = "approval_deferred"
    HUMAN_INPUT_REQUIRED = "human_input_required"
    HUMAN_REJECTED = "human_rejected"
    COMPLETION_REJECTED = "completion_rejected"
    CYCLE_LIMIT = "cycle_limit"
    ATTEMPT_LIMIT = "attempt_limit"
    STEP_LIMIT = "step_limit"
    CANCELLED = "cancelled"
    TIMED_OUT = "timed_out"
    COMPONENT_ERROR = "component_error"
    BUDGET_EXHAUSTED = "budget_exhausted"


class ResumeMode(str, Enum):
    """恢复运行时选择继续现有计划或强制重新规划。"""

    CONTINUE = "continue"
    REPLAN = "replan"


_ALLOWED_TRANSITIONS: dict[LoopStatus, frozenset[LoopStatus]] = {
    LoopStatus.CREATED: frozenset(
        {
            LoopStatus.PLANNING,
            LoopStatus.BLOCKED,
            LoopStatus.CANCELLED,
            LoopStatus.TIMED_OUT,
            LoopStatus.FAILED,
        }
    ),
    LoopStatus.PLANNING: frozenset(
        {
            LoopStatus.WAITING_APPROVAL,
            LoopStatus.EXECUTING,
            LoopStatus.BLOCKED,
            LoopStatus.CANCELLED,
            LoopStatus.TIMED_OUT,
            LoopStatus.FAILED,
        }
    ),
    LoopStatus.WAITING_APPROVAL: frozenset(
        {
            LoopStatus.EXECUTING,
            LoopStatus.PAUSED,
            LoopStatus.BLOCKED,
            LoopStatus.CANCELLED,
            LoopStatus.TIMED_OUT,
            LoopStatus.FAILED,
        }
    ),
    LoopStatus.EXECUTING: frozenset(
        {
            LoopStatus.PLANNING,
            LoopStatus.BLOCKED,
            LoopStatus.VERIFYING,
            LoopStatus.CANCELLED,
            LoopStatus.TIMED_OUT,
            LoopStatus.FAILED,
        }
    ),
    LoopStatus.VERIFYING: frozenset(
        {
            LoopStatus.PLANNING,
            LoopStatus.WAITING_APPROVAL,
            LoopStatus.EXECUTING,
            LoopStatus.COMPLETED,
            LoopStatus.PAUSED,
            LoopStatus.BLOCKED,
            LoopStatus.CANCELLED,
            LoopStatus.TIMED_OUT,
            LoopStatus.FAILED,
        }
    ),
    LoopStatus.PAUSED: frozenset(
        {
            LoopStatus.PLANNING,
            LoopStatus.WAITING_APPROVAL,
            LoopStatus.EXECUTING,
            LoopStatus.COMPLETED,
            LoopStatus.BLOCKED,
            LoopStatus.CANCELLED,
            LoopStatus.TIMED_OUT,
            LoopStatus.FAILED,
        }
    ),
    LoopStatus.COMPLETED: frozenset(),
    LoopStatus.BLOCKED: frozenset(
        {
            LoopStatus.PLANNING,
            LoopStatus.WAITING_APPROVAL,
            LoopStatus.EXECUTING,
            LoopStatus.CANCELLED,
            LoopStatus.TIMED_OUT,
            LoopStatus.FAILED,
        }
    ),
    LoopStatus.CANCELLED: frozenset(),
    LoopStatus.TIMED_OUT: frozenset(),
    LoopStatus.FAILED: frozenset(),
}


def ensure_transition(current: LoopStatus, target: LoopStatus) -> None:
    """校验一次生命周期状态转换是否合法。

    Args:
        current: 当前 Loop 状态。
        target: 请求进入的目标状态。

    Raises:
        InvalidStateTransitionError: 当目标状态无法从当前状态到达时抛出。
    """
    if target not in _ALLOWED_TRANSITIONS[current]:
        raise InvalidStateTransitionError(current=current.value, target=target.value)
