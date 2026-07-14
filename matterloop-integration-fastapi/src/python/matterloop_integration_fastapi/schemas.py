"""FastAPI 请求与响应模型。"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated

from matterloop_core import IterationRecord, LoopLimits, LoopRequest, LoopResult, ResumeMode
from matterloop_runtime import RunRecord
from pydantic import BaseModel, ConfigDict, Field, JsonValue, StringConstraints

NonEmptyText = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
RunIdentifier = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True,
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*$",
    ),
]


class _Schema(BaseModel):
    """为所有 API 模型提供一致的严格配置。"""

    model_config = ConfigDict(extra="forbid")


class LoopLimitsRequest(_Schema):
    """创建请求中的 Loop 安全边界。"""

    max_cycles: int = Field(default=5, ge=1)
    max_attempts: int = Field(default=20, ge=1)
    max_steps_per_plan: int = Field(default=20, ge=1)
    timeout_seconds: float | None = Field(default=None, gt=0)

    def to_domain(self) -> LoopLimits:
        """转换为不依赖 FastAPI 的核心值对象。"""
        return LoopLimits(
            max_cycles=self.max_cycles,
            max_attempts=self.max_attempts,
            max_steps_per_plan=self.max_steps_per_plan,
            timeout_seconds=self.timeout_seconds,
        )


class CreateLoopRequest(_Schema):
    """创建一次 Loop 运行的 HTTP 请求。"""

    goal: NonEmptyText
    acceptance_criteria: tuple[NonEmptyText, ...] = ()
    limits: LoopLimitsRequest = Field(default_factory=LoopLimitsRequest)
    metadata: dict[str, JsonValue] = Field(default_factory=dict)
    run_id: RunIdentifier | None = None

    def to_domain(self) -> LoopRequest:
        """转换为核心运行请求，并保留 JSON 元数据。"""
        return LoopRequest(
            goal=self.goal,
            acceptance_criteria=self.acceptance_criteria,
            limits=self.limits.to_domain(),
            metadata=self.metadata,
        )


class ResumeLoopRequest(_Schema):
    """恢复运行时选择精确继续或重新规划。"""

    mode: ResumeMode = ResumeMode.CONTINUE


class PlanStepResponse(_Schema):
    """审计记录中的计划步骤。"""

    step_id: str
    description: str
    executor: str
    acceptance_criteria: tuple[str, ...]
    requires_approval: bool


class ArtifactResponse(_Schema):
    """执行结果中的外部制品引用。"""

    name: str
    uri: str
    media_type: str | None


class ExecutionResponse(_Schema):
    """步骤执行输出和制品列表。"""

    output: str
    artifacts: tuple[ArtifactResponse, ...]


class VerificationResponse(_Schema):
    """步骤验证结论与证据。"""

    passed: bool
    feedback: str
    score: float | None
    evidence: tuple[str, ...]
    failed_criteria: tuple[str, ...]


class IterationResponse(_Schema):
    """一个已经完成执行与验证的步骤审计记录。"""

    cycle: int
    step_index: int
    attempt: int
    step: PlanStepResponse
    execution: ExecutionResponse
    verification: VerificationResponse


class RunResponse(_Schema):
    """统一表示直接运行结果和队列运行记录。"""

    run_id: str
    status: str
    output: str = ""
    cycles: int = 0
    total_attempts: int = 0
    completed_steps: int = 0
    records: tuple[IterationResponse, ...] = ()
    stop_reason: str | None = None
    error: str = ""
    goal: str | None = None
    version: int | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None

    @classmethod
    def from_result(cls, result: LoopResult) -> RunResponse:
        """把核心结果映射为稳定 HTTP 响应。"""
        return cls(
            run_id=result.run_id,
            status=result.status.value,
            output=result.output,
            cycles=result.cycles,
            total_attempts=result.total_attempts,
            completed_steps=result.completed_steps,
            records=tuple(_record_response(record) for record in result.records),
            stop_reason=result.stop_reason.value if result.stop_reason is not None else None,
            error=result.error,
        )

    @classmethod
    def from_record(cls, record: RunRecord) -> RunResponse:
        """把队列记录及其可选最终结果映射为统一响应。"""
        result = record.result
        response = (
            cls.from_result(result)
            if result is not None
            else cls(
                run_id=record.run_id,
                status=record.status.value,
            )
        )
        return response.model_copy(
            update={
                "run_id": record.run_id,
                "status": record.status.value,
                "goal": record.request.goal,
                "version": record.version,
                "created_at": record.created_at,
                "updated_at": record.updated_at,
                "error": record.error or response.error,
            }
        )


class ResumeResponse(_Schema):
    """恢复请求的接受状态和最新运行视图。"""

    accepted: bool
    run: RunResponse


class CancelResponse(_Schema):
    """取消请求是否被运行时接受。"""

    run_id: str
    accepted: bool


class EventListResponse(_Schema):
    """保持事件发布器字段的只读分页结果。"""

    items: tuple[dict[str, object], ...]


def _record_response(record: IterationRecord) -> IterationResponse:
    """把核心审计记录转换为不泄漏内部元数据的响应。"""
    return IterationResponse(
        cycle=record.cycle,
        step_index=record.step_index,
        attempt=record.attempt,
        step=PlanStepResponse(
            step_id=record.step.step_id,
            description=record.step.description,
            executor=record.step.executor,
            acceptance_criteria=record.step.acceptance_criteria,
            requires_approval=record.step.requires_approval,
        ),
        execution=ExecutionResponse(
            output=record.execution.output,
            artifacts=tuple(
                ArtifactResponse(
                    name=artifact.name,
                    uri=artifact.uri,
                    media_type=artifact.media_type,
                )
                for artifact in record.execution.artifacts
            ),
        ),
        verification=VerificationResponse(
            passed=record.verification.passed,
            feedback=record.verification.feedback,
            score=record.verification.score,
            evidence=record.verification.evidence,
            failed_criteria=record.verification.failed_criteria,
        ),
    )


__all__ = [
    "ArtifactResponse",
    "CancelResponse",
    "CreateLoopRequest",
    "EventListResponse",
    "ExecutionResponse",
    "IterationResponse",
    "LoopLimitsRequest",
    "PlanStepResponse",
    "ResumeLoopRequest",
    "ResumeResponse",
    "RunResponse",
    "VerificationResponse",
]
