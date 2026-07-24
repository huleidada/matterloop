"""Loop 输入、计划、执行证据与运行上下文值对象。"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from math import isfinite
from types import MappingProxyType
from uuid import uuid4

from matterloop_core.context.human import HumanInteractionRecord, HumanInteractionRequest
from matterloop_core.state import LoopStatus, StopReason


def _frozen_mapping(value: Mapping[str, object]) -> Mapping[str, object]:
    """复制并冻结只读元数据，避免调用方在运行中修改审计信息。"""
    return MappingProxyType(dict(value))


def _validate_text(value: str, field_name: str) -> None:
    """校验需要作为稳定标识或描述使用的文本。"""
    if not value.strip():
        raise ValueError(f"{field_name} must not be empty")


@dataclass(frozen=True, slots=True)
class LoopLimits:
    """定义一次 Loop 的三类独立预算和总运行时限。

    Args:
        max_cycles: 最多允许规划多少轮；一次重新规划会开始新一轮。
        max_attempts: 最多允许调用执行器多少次，失败重试同样计数。
        max_steps_per_plan: 单个计划最多可以包含多少个步骤。
        timeout_seconds: 从首次启动起计算的可选总运行超时秒数。
    """

    max_cycles: int = 5
    max_attempts: int = 20
    max_steps_per_plan: int = 20
    timeout_seconds: float | None = None

    def __post_init__(self) -> None:
        """拒绝无法形成安全运行边界的预算值。"""
        if self.max_cycles < 1:
            raise ValueError("max_cycles must be at least 1")
        if self.max_attempts < 1:
            raise ValueError("max_attempts must be at least 1")
        if self.max_steps_per_plan < 1:
            raise ValueError("max_steps_per_plan must be at least 1")
        if self.timeout_seconds is not None and (
            not isfinite(self.timeout_seconds) or self.timeout_seconds <= 0
        ):
            raise ValueError("timeout_seconds must be finite and greater than 0")


@dataclass(frozen=True, slots=True)
class LoopRequest:
    """描述一个具有明确验收条件和执行边界的 Loop 目标。

    Args:
        goal: Loop 必须实现的、便于人类理解的目标。
        acceptance_criteria: 判定整个目标完成所需满足的条件。
        limits: 规划轮次、执行尝试、计划步骤和时间预算。
        metadata: 内核原样传递、不参与业务解释的关联数据。
    """

    goal: str
    acceptance_criteria: tuple[str, ...] = ()
    limits: LoopLimits = field(default_factory=LoopLimits)
    metadata: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        """在调用外部组件前校验关键输入并冻结元数据。"""
        _validate_text(self.goal, "goal")
        if any(not criterion.strip() for criterion in self.acceptance_criteria):
            raise ValueError("acceptance_criteria must not contain empty values")
        object.__setattr__(self, "metadata", _frozen_mapping(self.metadata))


@dataclass(frozen=True, slots=True)
class PlanStep:
    """表示一个可以独立执行、验证和审批的工作单元。

    Args:
        description: 执行器需要完成的具体工作。
        executor: 每次执行时从注册中心解析的执行器名称。
        acceptance_criteria: 只适用于本步骤的验收条件。
        requires_approval: 是否必须在执行前通过审批门。
        step_id: 计划内稳定且可审计的步骤标识。
    """

    description: str
    executor: str = "default"
    acceptance_criteria: tuple[str, ...] = ()
    requires_approval: bool = False
    step_id: str = field(default_factory=lambda: uuid4().hex)

    def __post_init__(self) -> None:
        """保证步骤能够被明确选择和执行。"""
        _validate_text(self.description, "description")
        _validate_text(self.executor, "executor")
        _validate_text(self.step_id, "step_id")
        if any(not criterion.strip() for criterion in self.acceptance_criteria):
            raise ValueError("acceptance_criteria must not contain empty values")


@dataclass(frozen=True, slots=True)
class Plan:
    """保存规划器生成的有序工作步骤。"""

    steps: tuple[PlanStep, ...]


@dataclass(frozen=True, slots=True)
class ArtifactRef:
    """引用执行器产生的外部制品，而不把大对象塞入检查点。

    Args:
        name: 便于人类和验证器识别的制品名称。
        uri: 由对应存储实现解释的稳定位置。
        media_type: 可选的 IANA 媒体类型。
        metadata: 与制品相关的只读扩展信息。
    """

    name: str
    uri: str
    media_type: str | None = None
    metadata: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        """校验制品引用并冻结其扩展信息。"""
        _validate_text(self.name, "name")
        _validate_text(self.uri, "uri")
        if self.media_type is not None:
            _validate_text(self.media_type, "media_type")
        object.__setattr__(self, "metadata", _frozen_mapping(self.metadata))


@dataclass(frozen=True, slots=True)
class ExecutionResult:
    """保存执行器输出与制品证据，但不负责判断结果是否正确。"""

    output: str
    artifacts: tuple[ArtifactRef, ...] = ()
    metadata: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        """冻结执行器附带的扩展信息。"""
        object.__setattr__(self, "metadata", _frozen_mapping(self.metadata))


@dataclass(frozen=True, slots=True)
class VerificationResult:
    """记录独立验证结论、证据、评分和可执行反馈。

    `score` 使用 0 到 100 的统一区间；没有可比较评分时保持为 ``None``。
    """

    passed: bool
    feedback: str = ""
    score: float | None = None
    evidence: tuple[str, ...] = ()
    failed_criteria: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        """保证评分区间明确且证据条目不为空。"""
        if self.score is not None and not 0 <= self.score <= 100:
            raise ValueError("score must be between 0 and 100")
        if any(not item.strip() for item in self.evidence):
            raise ValueError("evidence must not contain empty values")
        if any(not item.strip() for item in self.failed_criteria):
            raise ValueError("failed_criteria must not contain empty values")
        if self.passed and self.failed_criteria:
            raise ValueError("passed verification must not contain failed criteria")


@dataclass(frozen=True, slots=True)
class IterationRecord:
    """保存一个步骤在某轮中完成执行与验证后的不可变证据。"""

    cycle: int
    step_index: int
    step: PlanStep
    execution: ExecutionResult
    verification: VerificationResult
    attempt: int = 1

    def __post_init__(self) -> None:
        """拒绝无法对应实际运行位置的记录。"""
        if self.cycle < 1:
            raise ValueError("cycle must be at least 1")
        if self.step_index < 0:
            raise ValueError("step_index must not be negative")
        if self.attempt < 1:
            raise ValueError("attempt must be at least 1")


@dataclass(slots=True)
class LoopContext:
    """保存仅由 Loop 控制器管理的可变运行状态。"""

    request: LoopRequest
    run_id: str = field(default_factory=lambda: uuid4().hex)
    status: LoopStatus = LoopStatus.CREATED
    records: list[IterationRecord] = field(default_factory=list)
    feedback: str = ""
    current_plan: Plan | None = None
    current_step_index: int = 0
    cycle_count: int = 0
    total_attempts: int = 0
    completed_steps: int = 0
    stop_reason: StopReason | None = None
    error: str = ""
    pending_interaction: HumanInteractionRequest | None = None
    human_interactions: list[HumanInteractionRecord] = field(default_factory=list)
    approved_step_ids: set[str] = field(default_factory=set)
    replan_required: bool = False
    completion_approved: bool = False
    active_operation_id: str | None = None
    pending_execution: ExecutionResult | None = None
    pending_attempt: int | None = None
    last_heartbeat_at: datetime | None = None
    event_sequence: int = 0
    revision: int = 0
    active_elapsed_seconds: float = 0
    active_started_at: datetime | None = None
    propagation_context: dict[str, str] = field(default_factory=dict)
    started_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    @property
    def feedback_history(self) -> tuple[HumanInteractionRecord, ...]:
        """返回不可变的完整人工反馈历史。"""
        return tuple(self.human_interactions)

    def snapshot(self) -> LoopContext:
        """返回供持久化和观察者使用的隔离状态快照。"""
        return LoopContext(
            request=self.request,
            run_id=self.run_id,
            status=self.status,
            records=list(self.records),
            feedback=self.feedback,
            current_plan=self.current_plan,
            current_step_index=self.current_step_index,
            cycle_count=self.cycle_count,
            total_attempts=self.total_attempts,
            completed_steps=self.completed_steps,
            stop_reason=self.stop_reason,
            error=self.error,
            pending_interaction=self.pending_interaction,
            human_interactions=list(self.human_interactions),
            approved_step_ids=set(self.approved_step_ids),
            replan_required=self.replan_required,
            completion_approved=self.completion_approved,
            active_operation_id=self.active_operation_id,
            pending_execution=self.pending_execution,
            pending_attempt=self.pending_attempt,
            last_heartbeat_at=self.last_heartbeat_at,
            event_sequence=self.event_sequence,
            revision=self.revision,
            active_elapsed_seconds=self.active_elapsed_seconds,
            active_started_at=self.active_started_at,
            propagation_context=dict(self.propagation_context),
            started_at=self.started_at,
            updated_at=self.updated_at,
        )


@dataclass(frozen=True, slots=True)
class LoopResult:
    """对外提供不可变的终态结果和完整审计轨迹。"""

    run_id: str
    status: LoopStatus
    output: str
    cycles: int
    total_attempts: int
    completed_steps: int
    records: tuple[IterationRecord, ...]
    stop_reason: StopReason | None
    error: str = ""
    pending_interaction: HumanInteractionRequest | None = None
    human_interactions: tuple[HumanInteractionRecord, ...] = ()
    active_operation_id: str | None = None
    last_heartbeat_at: datetime | None = None
    revision: int = 0
    event_sequence: int = 0

    @property
    def iterations(self) -> int:
        """返回已有执行与验证记录数，便于展示运行进度。"""
        return len(self.records)

    @property
    def feedback_history(self) -> tuple[HumanInteractionRecord, ...]:
        """返回本次运行已处理的全部人工交互记录。"""
        return self.human_interactions


def result_from_context(context: LoopContext) -> LoopResult:
    """从隔离上下文安全构造公开结果，不泄漏可变记录集合。"""
    output = context.records[-1].execution.output if context.records else ""
    return LoopResult(
        run_id=context.run_id,
        status=context.status,
        output=output,
        cycles=context.cycle_count,
        total_attempts=context.total_attempts,
        completed_steps=context.completed_steps,
        records=tuple(context.records),
        stop_reason=context.stop_reason,
        error=context.error,
        pending_interaction=context.pending_interaction,
        human_interactions=tuple(context.human_interactions),
        active_operation_id=context.active_operation_id,
        last_heartbeat_at=context.last_heartbeat_at,
        revision=context.revision,
        event_sequence=context.event_sequence,
    )
