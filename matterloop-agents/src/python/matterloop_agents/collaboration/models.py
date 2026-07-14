"""多智能体任务图、运行快照和结果值对象。"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from uuid import uuid4

from matterloop_core import (
    ArtifactRef,
    HumanInteractionRecord,
    HumanInteractionRequest,
)

from matterloop_agents.collaboration._immutability import freeze_mapping


def _validate_text(value: str, field_name: str) -> None:
    """校验稳定标识和面向 Agent 的文本。"""
    if not value.strip():
        raise ValueError(f"{field_name} must not be empty")


def _freeze_metadata(value: Mapping[str, object]) -> Mapping[str, object]:
    """复制并冻结调用方元数据，隔离运行中的外部修改。"""
    return freeze_mapping(value)


class TeamStatus(str, Enum):
    """一次团队协作运行的生命周期状态。"""

    CREATED = "created"
    PLANNING = "planning"
    RUNNING = "running"
    WAITING_APPROVAL = "waiting_approval"
    PAUSED = "paused"
    BLOCKED = "blocked"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    TIMED_OUT = "timed_out"

    @property
    def is_terminal(self) -> bool:
        """判断团队运行是否已经进入不可恢复的终态。"""
        return self in {self.COMPLETED, self.FAILED, self.CANCELLED, self.TIMED_OUT}


class TaskStatus(str, Enum):
    """任务节点在 DAG 中的执行状态。"""

    PENDING = "pending"
    READY = "ready"
    WAITING_APPROVAL = "waiting_approval"
    RUNNING = "running"
    VERIFYING = "verifying"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    BLOCKED = "blocked"
    CANCELLED = "cancelled"

    @property
    def is_terminal(self) -> bool:
        """判断任务是否不再允许继续执行。"""
        return self in {self.SUCCEEDED, self.FAILED, self.BLOCKED, self.CANCELLED}


class TeamStopReason(str, Enum):
    """团队运行停止或暂停的结构化原因。"""

    COMPLETED = "completed"
    APPROVAL_DEFERRED = "approval_deferred"
    APPROVAL_REJECTED = "approval_rejected"
    TASK_FAILED = "task_failed"
    NO_CAPABLE_AGENT = "no_capable_agent"
    AGENT_CAPACITY = "agent_capacity"
    DEADLOCK = "deadlock"
    CANCELLED = "cancelled"
    TIMED_OUT = "timed_out"
    COMPONENT_ERROR = "component_error"
    CYCLE_LIMIT = "cycle_limit"
    PLAN_REVISION_LIMIT = "plan_revision_limit"
    REVIEW_STOPPED = "review_stopped"
    HUMAN_REJECTED = "human_rejected"
    BUDGET_EXHAUSTED = "budget_exhausted"


@dataclass(frozen=True, slots=True)
class TeamLimits:
    """限制团队循环、任务数量、并发、重试和活跃时间。

    Args:
        max_tasks: 单次规划允许生成的最多任务数。
        max_concurrency: 同时执行的最多任务数。
        max_task_attempts: 每个任务允许的最多执行次数。
        max_cycles: 团队外层“规划—执行—审查”允许的最多循环数。
        max_plan_revisions: 人工修订或团队审查允许触发的最多重规划次数。
        timeout_seconds: 团队运行的可选活跃超时秒数，人工等待不计入。
    """

    max_tasks: int = 50
    max_concurrency: int = 4
    max_task_attempts: int = 3
    max_cycles: int = 3
    max_plan_revisions: int = 2
    timeout_seconds: float | None = None

    def __post_init__(self) -> None:
        """拒绝不能形成有效执行边界的限制。"""
        if self.max_tasks < 1:
            raise ValueError("max_tasks must be at least 1")
        if self.max_concurrency < 1:
            raise ValueError("max_concurrency must be at least 1")
        if self.max_task_attempts < 1:
            raise ValueError("max_task_attempts must be at least 1")
        if self.max_cycles < 1:
            raise ValueError("max_cycles must be at least 1")
        if self.max_plan_revisions < 0:
            raise ValueError("max_plan_revisions must not be negative")
        if self.timeout_seconds is not None and self.timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be greater than 0")


@dataclass(frozen=True, slots=True)
class TeamRequest:
    """描述一个需要多个 Agent 协作完成的目标。

    Args:
        goal: 团队需要完成的总体目标。
        acceptance_criteria: 判断团队结果完成所需满足的条件。
        limits: 任务、并发、重试和超时边界。
        metadata: 协作层原样传递的只读关联数据。
    """

    goal: str
    acceptance_criteria: tuple[str, ...] = ()
    limits: TeamLimits = field(default_factory=TeamLimits)
    metadata: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        """校验团队目标并冻结元数据。"""
        _validate_text(self.goal, "goal")
        if any(not item.strip() for item in self.acceptance_criteria):
            raise ValueError("acceptance_criteria must not contain empty values")
        object.__setattr__(self, "metadata", _freeze_metadata(self.metadata))


@dataclass(frozen=True, slots=True)
class TaskSpec:
    """描述任务图中的一个可独立分配节点。

    Args:
        task_id: 团队运行内唯一且稳定的任务标识。
        description: Agent 需要完成的具体工作。
        capability: 执行该任务必须具备的能力标签。
        dependencies: 必须先成功的任务标识。
        acceptance_criteria: 任务级验收条件。
        requires_approval: 是否必须在分配前经过审批。
        priority: 同时就绪时的调度优先级，数值越大越优先。
        metadata: 只读任务扩展信息。
    """

    task_id: str
    description: str
    capability: str
    dependencies: tuple[str, ...] = ()
    acceptance_criteria: tuple[str, ...] = ()
    requires_approval: bool = False
    priority: int = 0
    metadata: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        """规范任务标识、依赖和扩展数据。"""
        _validate_text(self.task_id, "task_id")
        _validate_text(self.description, "description")
        _validate_text(self.capability, "capability")
        if any(not item.strip() for item in self.dependencies):
            raise ValueError("dependencies must not contain empty values")
        if len(self.dependencies) != len(set(self.dependencies)):
            raise ValueError("dependencies must not contain duplicates")
        if self.task_id in self.dependencies:
            raise ValueError("task must not depend on itself")
        if any(not item.strip() for item in self.acceptance_criteria):
            raise ValueError("acceptance_criteria must not contain empty values")
        object.__setattr__(self, "metadata", _freeze_metadata(self.metadata))


@dataclass(frozen=True, slots=True)
class TaskResult:
    """保存 Agent 对一个任务的结构化执行结果。"""

    task_id: str
    agent_id: str
    success: bool
    output: str = ""
    artifacts: tuple[ArtifactRef, ...] = ()
    error: str = ""
    attempt: int = 1
    metadata: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        """校验结果归属、尝试次数并冻结元数据。"""
        _validate_text(self.task_id, "task_id")
        _validate_text(self.agent_id, "agent_id")
        if self.attempt < 1:
            raise ValueError("attempt must be at least 1")
        if self.success and self.error:
            raise ValueError("successful task result must not contain an error")
        object.__setattr__(self, "metadata", _freeze_metadata(self.metadata))


@dataclass(frozen=True, slots=True)
class TaskVerification:
    """记录独立验证器对任务结果的判断。"""

    passed: bool
    feedback: str = ""
    score: float | None = None
    evidence: tuple[str, ...] = ()
    failed_criteria: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        """保证评分、证据和失败条件相互一致。"""
        if self.score is not None and not 0 <= self.score <= 100:
            raise ValueError("score must be between 0 and 100")
        if any(not item.strip() for item in self.evidence):
            raise ValueError("evidence must not contain empty values")
        if any(not item.strip() for item in self.failed_criteria):
            raise ValueError("failed_criteria must not contain empty values")
        if self.passed and self.failed_criteria:
            raise ValueError("passed verification must not contain failed criteria")


class TeamReviewAction(str, Enum):
    """团队级审查对外层循环的结构化决策。"""

    ACCEPT = "accept"
    REPLAN = "replan"
    REQUEST_HUMAN = "request_human"
    STOP = "stop"


@dataclass(frozen=True, slots=True)
class TeamReview:
    """保存团队级目标验收和后续动作。

    Args:
        action: 验收后要执行的外层动作。
        feedback: 传给下一轮规划或返回给调用方的说明。
        score: 可选的零到一百评分。
        evidence: 支持审查结论的证据。
        failed_criteria: 未满足的总体验收条件。
        interaction: 请求人工判断时的结构化交互。
    """

    action: TeamReviewAction
    feedback: str = ""
    score: float | None = None
    evidence: tuple[str, ...] = ()
    failed_criteria: tuple[str, ...] = ()
    interaction: HumanInteractionRequest | None = None

    def __post_init__(self) -> None:
        """校验审查分数、文本和人工交互的一致性。"""
        if self.score is not None and not 0 <= self.score <= 100:
            raise ValueError("score must be between 0 and 100")
        if any(not item.strip() for item in self.evidence):
            raise ValueError("evidence must not contain empty values")
        if any(not item.strip() for item in self.failed_criteria):
            raise ValueError("failed_criteria must not contain empty values")
        if self.action is TeamReviewAction.REQUEST_HUMAN and self.interaction is None:
            raise ValueError("REQUEST_HUMAN review requires an interaction")
        if self.action is not TeamReviewAction.REQUEST_HUMAN and self.interaction is not None:
            raise ValueError("only REQUEST_HUMAN review may contain an interaction")


@dataclass(frozen=True, slots=True)
class AgentSpec:
    """描述一个可调度 Agent 的能力与并发边界。"""

    agent_id: str
    capabilities: frozenset[str]
    max_concurrency: int = 1
    version: str = "0.1.0"
    description: str = ""
    role: str = "worker"
    metadata: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        """规范 Agent 标识、能力和不可变元数据。"""
        _validate_text(self.agent_id, "agent_id")
        _validate_text(self.version, "version")
        _validate_text(self.role, "role")
        if not self.capabilities or any(not item.strip() for item in self.capabilities):
            raise ValueError("capabilities must contain non-empty values")
        if self.max_concurrency < 1:
            raise ValueError("max_concurrency must be at least 1")
        object.__setattr__(
            self,
            "capabilities",
            frozenset(item.strip() for item in self.capabilities),
        )
        object.__setattr__(self, "metadata", _freeze_metadata(self.metadata))


@dataclass(frozen=True, slots=True)
class TeamPlanningContext:
    """向规划器提供当前循环和可用能力的完整快照。

    Args:
        run_id: 团队运行标识。
        request: 原始团队目标和边界。
        cycle: 将要规划的循环序号，从一开始。
        plan_revision: 已发生的重规划次数。
        available_agents: 当次规划可见的 Agent 能力目录快照。
        prior_reviews: 已完成循环的团队审查历史。
        human_feedback: 已提交的结构化人工反馈历史。
    """

    run_id: str
    request: TeamRequest
    cycle: int
    plan_revision: int
    available_agents: tuple[AgentSpec, ...]
    prior_reviews: tuple[TeamReview, ...] = ()
    human_feedback: tuple[HumanInteractionRecord, ...] = ()

    def __post_init__(self) -> None:
        """校验规划关联标识和单调计数。"""
        _validate_text(self.run_id, "run_id")
        if self.cycle < 1:
            raise ValueError("cycle must be at least 1")
        if self.plan_revision < 0:
            raise ValueError("plan_revision must not be negative")

    @property
    def available_capabilities(self) -> frozenset[str]:
        """返回当前目录中所有可用能力标签。"""
        return frozenset(
            capability for agent in self.available_agents for capability in agent.capabilities
        )


@dataclass(frozen=True, slots=True)
class TeamReviewContext:
    """向团队审查器提供一次循环的可验收草稿。"""

    run_id: str
    request: TeamRequest
    cycle: int
    plan_revision: int
    task_results: tuple[TaskResult, ...]
    draft_output: str
    prior_reviews: tuple[TeamReview, ...] = ()
    human_feedback: tuple[HumanInteractionRecord, ...] = ()

    def __post_init__(self) -> None:
        """校验审查运行标识和计数。"""
        _validate_text(self.run_id, "run_id")
        if self.cycle < 1:
            raise ValueError("cycle must be at least 1")
        if self.plan_revision < 0:
            raise ValueError("plan_revision must not be negative")


@dataclass(frozen=True, slots=True)
class AgentTaskContext:
    """向被分配 Agent 提供隔离后的任务执行上下文。"""

    team_run_id: str
    request: TeamRequest
    task: TaskSpec
    agent_id: str
    attempt: int
    dependency_results: tuple[TaskResult, ...] = ()
    previous_error: str = ""
    human_feedback: tuple[HumanInteractionRecord, ...] = ()

    def __post_init__(self) -> None:
        """校验上下文关联标识和尝试次数。"""
        _validate_text(self.team_run_id, "team_run_id")
        _validate_text(self.agent_id, "agent_id")
        if self.attempt < 1:
            raise ValueError("attempt must be at least 1")


@dataclass(frozen=True, slots=True)
class TaskState:
    """保存任务节点的不可变运行状态。"""

    spec: TaskSpec
    status: TaskStatus = TaskStatus.PENDING
    attempt: int = 0
    approval_granted: bool = False
    assigned_agent: str | None = None
    result: TaskResult | None = None
    verification: TaskVerification | None = None
    error: str = ""

    def __post_init__(self) -> None:
        """拒绝与任务状态不一致的记录。"""
        if self.attempt < 0:
            raise ValueError("attempt must not be negative")
        if self.assigned_agent is not None:
            _validate_text(self.assigned_agent, "assigned_agent")
        if self.result is not None and self.result.task_id != self.spec.task_id:
            raise ValueError("task result belongs to another task")
        if self.status in {TaskStatus.RUNNING, TaskStatus.VERIFYING} and (
            self.attempt < 1 or self.assigned_agent is None
        ):
            raise ValueError("inflight task must contain an attempt and assigned agent")
        if self.status is TaskStatus.VERIFYING and (self.result is None or not self.result.success):
            raise ValueError("verifying task must contain a successful result")
        if self.status is TaskStatus.SUCCEEDED and (
            self.result is None
            or not self.result.success
            or self.verification is None
            or not self.verification.passed
        ):
            raise ValueError("succeeded task must contain a successful result")


@dataclass(frozen=True, slots=True)
class TeamCycleRecord:
    """保存一次外层循环的计划、草稿和审查证据。"""

    cycle: int
    plan_revision: int
    tasks: tuple[TaskState, ...]
    draft_output: str = ""
    review: TeamReview | None = None
    error: str = ""

    def __post_init__(self) -> None:
        """拒绝无效循环序号和修订计数。"""
        if self.cycle < 1:
            raise ValueError("cycle must be at least 1")
        if self.plan_revision < 0:
            raise ValueError("plan_revision must not be negative")


@dataclass(frozen=True, slots=True)
class TeamSnapshot:
    """提供给仓储、恢复和观察者的不可变团队快照。"""

    request: TeamRequest
    tasks: tuple[TaskState, ...]
    run_id: str = field(default_factory=lambda: uuid4().hex)
    status: TeamStatus = TeamStatus.CREATED
    version: int = 0
    stop_reason: TeamStopReason | None = None
    output: str = ""
    error: str = ""
    cycle: int = 0
    plan_revision: int = 0
    cycle_history: tuple[TeamCycleRecord, ...] = ()
    pending_interaction: HumanInteractionRequest | None = None
    pending_review: TeamReview | None = None
    human_interactions: tuple[HumanInteractionRecord, ...] = ()
    review_approved_cycle: int | None = None
    active_elapsed_seconds: float = 0.0
    active_started_at: datetime | None = None
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def __post_init__(self) -> None:
        """校验快照标识、版本和任务唯一性。"""
        _validate_text(self.run_id, "run_id")
        if self.version < 0:
            raise ValueError("version must not be negative")
        if self.cycle < 0:
            raise ValueError("cycle must not be negative")
        if self.plan_revision < 0:
            raise ValueError("plan_revision must not be negative")
        if self.review_approved_cycle is not None and self.review_approved_cycle < 1:
            raise ValueError("review_approved_cycle must be at least 1")
        if self.active_elapsed_seconds < 0:
            raise ValueError("active_elapsed_seconds must not be negative")
        task_ids = [state.spec.task_id for state in self.tasks]
        if len(task_ids) != len(set(task_ids)):
            raise ValueError("snapshot contains duplicate task identifiers")

    @property
    def feedback_history(self) -> tuple[HumanInteractionRecord, ...]:
        """返回与 Core Loop 同名的人工反馈历史别名。"""
        return self.human_interactions


@dataclass(frozen=True, slots=True)
class TeamResult:
    """团队运行对外返回的不可变终态或暂停结果。"""

    run_id: str
    status: TeamStatus
    task_results: tuple[TaskResult, ...]
    output: str = ""
    stop_reason: TeamStopReason | None = None
    error: str = ""
    cycle: int = 0
    cycle_history: tuple[TeamCycleRecord, ...] = ()
    pending_interaction: HumanInteractionRequest | None = None
    human_interactions: tuple[HumanInteractionRecord, ...] = ()
    started_at: datetime | None = None
    finished_at: datetime | None = None

    @property
    def completed_tasks(self) -> int:
        """返回已经成功完成的任务数量。"""
        return len(self.task_results)

    @property
    def feedback_history(self) -> tuple[HumanInteractionRecord, ...]:
        """返回团队运行中的完整人工反馈历史。"""
        return self.human_interactions


__all__ = [
    "AgentSpec",
    "AgentTaskContext",
    "TaskResult",
    "TaskSpec",
    "TaskState",
    "TaskStatus",
    "TaskVerification",
    "TeamCycleRecord",
    "TeamLimits",
    "TeamPlanningContext",
    "TeamRequest",
    "TeamReview",
    "TeamReviewAction",
    "TeamReviewContext",
    "TeamResult",
    "TeamSnapshot",
    "TeamStatus",
    "TeamStopReason",
]
