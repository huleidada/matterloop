"""带版本的 Loop 检查点 JSON 编解码器。"""

from __future__ import annotations

import json
import math
from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import cast

from matterloop_core.context.human import (
    HumanAction,
    HumanInteractionKind,
    HumanInteractionRecord,
    HumanInteractionRequest,
    HumanResponse,
)
from matterloop_core.context.models import (
    ArtifactRef,
    ExecutionResult,
    IterationRecord,
    LoopContext,
    LoopLimits,
    LoopRequest,
    Plan,
    PlanStep,
    VerificationResult,
)
from matterloop_core.exceptions import CheckpointSchemaError
from matterloop_core.state import LoopStatus, StopReason


class LoopCheckpointCodec:
    """把上下文转换为可跨进程保存的版本化 JSON 数据。

    编码结果只包含 JSON 标准类型。解码器严格拒绝未知版本和错误字段类型，避免把损坏
    或来自未来版本的数据静默解释成当前状态。
    """

    schema_version = 2

    def encode(self, context: LoopContext) -> dict[str, object]:
        """把上下文编码为可以直接交给 ``json.dumps`` 的字典。"""
        return {
            "schema_version": self.schema_version,
            "context": {
                "request": self._encode_request(context.request),
                "run_id": context.run_id,
                "status": context.status.value,
                "records": [self._encode_record(record) for record in context.records],
                "feedback": context.feedback,
                "current_plan": (
                    self._encode_plan(context.current_plan)
                    if context.current_plan is not None
                    else None
                ),
                "current_step_index": context.current_step_index,
                "cycle_count": context.cycle_count,
                "total_attempts": context.total_attempts,
                "completed_steps": context.completed_steps,
                "stop_reason": (
                    context.stop_reason.value if context.stop_reason is not None else None
                ),
                "error": context.error,
                "pending_interaction": (
                    self._encode_interaction(context.pending_interaction)
                    if context.pending_interaction is not None
                    else None
                ),
                "human_interactions": [
                    self._encode_human_record(record) for record in context.human_interactions
                ],
                "approved_step_ids": sorted(context.approved_step_ids),
                "replan_required": context.replan_required,
                "completion_approved": context.completion_approved,
                "active_operation_id": context.active_operation_id,
                "pending_execution": (
                    self._encode_execution(context.pending_execution)
                    if context.pending_execution is not None
                    else None
                ),
                "pending_attempt": context.pending_attempt,
                "last_heartbeat_at": (
                    context.last_heartbeat_at.isoformat()
                    if context.last_heartbeat_at is not None
                    else None
                ),
                "event_sequence": context.event_sequence,
                "revision": context.revision,
                "active_elapsed_seconds": context.active_elapsed_seconds,
                "active_started_at": (
                    context.active_started_at.isoformat()
                    if context.active_started_at is not None
                    else None
                ),
                "started_at": context.started_at.isoformat(),
                "updated_at": context.updated_at.isoformat(),
            },
        }

    def decode(self, payload: Mapping[str, object]) -> LoopContext:
        """从已解析的 JSON 字典恢复上下文。

        Raises:
            CheckpointSchemaError: 当版本不支持或字段结构无效时抛出。
        """
        try:
            version = self._integer(payload.get("schema_version"), "schema_version")
            if version != self.schema_version:
                raise CheckpointSchemaError(f"unsupported checkpoint schema version: {version}")
            data = self._mapping(payload.get("context"), "context")
            request = self._decode_request(self._mapping(data.get("request"), "request"))
            records = [
                self._decode_record(self._mapping(item, "records item"))
                for item in self._sequence(data.get("records"), "records")
            ]
            raw_plan = data.get("current_plan")
            raw_pending = data.get("pending_interaction")
            raw_pending_execution = data.get("pending_execution")
            context = LoopContext(
                request=request,
                run_id=self._text(data.get("run_id"), "run_id"),
                status=LoopStatus(self._text(data.get("status"), "status")),
                records=records,
                feedback=self._text(data.get("feedback"), "feedback", allow_empty=True),
                current_plan=(
                    None
                    if raw_plan is None
                    else self._decode_plan(self._mapping(raw_plan, "current_plan"))
                ),
                current_step_index=self._integer(
                    data.get("current_step_index"), "current_step_index"
                ),
                cycle_count=self._integer(data.get("cycle_count"), "cycle_count"),
                total_attempts=self._integer(data.get("total_attempts"), "total_attempts"),
                completed_steps=self._integer(data.get("completed_steps"), "completed_steps"),
                stop_reason=self._optional_stop_reason(data.get("stop_reason")),
                error=self._text(data.get("error"), "error", allow_empty=True),
                pending_interaction=(
                    None
                    if raw_pending is None
                    else self._decode_interaction(self._mapping(raw_pending, "pending_interaction"))
                ),
                human_interactions=[
                    self._decode_human_record(self._mapping(item, "human_interactions item"))
                    for item in self._sequence(data.get("human_interactions"), "human_interactions")
                ],
                approved_step_ids=set(
                    self._string_tuple(data.get("approved_step_ids"), "approved_step_ids")
                ),
                replan_required=self._boolean(data.get("replan_required"), "replan_required"),
                completion_approved=self._boolean(
                    data.get("completion_approved"), "completion_approved"
                ),
                active_operation_id=self._optional_text(
                    data.get("active_operation_id"), "active_operation_id"
                ),
                pending_execution=(
                    None
                    if raw_pending_execution is None
                    else self._decode_execution(
                        self._mapping(raw_pending_execution, "pending_execution")
                    )
                ),
                pending_attempt=self._optional_integer(
                    data.get("pending_attempt"), "pending_attempt"
                ),
                last_heartbeat_at=self._optional_datetime(
                    data.get("last_heartbeat_at"), "last_heartbeat_at"
                ),
                event_sequence=self._integer(data.get("event_sequence"), "event_sequence"),
                revision=self._integer(data.get("revision"), "revision"),
                active_elapsed_seconds=self._number(
                    data.get("active_elapsed_seconds"), "active_elapsed_seconds"
                ),
                active_started_at=self._optional_datetime(
                    data.get("active_started_at"), "active_started_at"
                ),
                started_at=self._datetime(data.get("started_at"), "started_at"),
                updated_at=self._datetime(data.get("updated_at"), "updated_at"),
            )
            self._validate_context(context)
            return context
        except CheckpointSchemaError:
            raise
        except (TypeError, ValueError, KeyError) as exc:
            raise CheckpointSchemaError(f"invalid checkpoint: {exc}") from exc

    def dumps(self, context: LoopContext, *, indent: int | None = None) -> str:
        """把上下文编码成不包含非标准数字的 JSON 文本。"""
        try:
            return json.dumps(
                self.encode(context),
                ensure_ascii=False,
                allow_nan=False,
                indent=indent,
                separators=None if indent is not None else (",", ":"),
            )
        except (TypeError, ValueError) as exc:
            raise CheckpointSchemaError(f"checkpoint is not JSON serializable: {exc}") from exc

    def loads(self, payload: str) -> LoopContext:
        """解析 JSON 文本并恢复上下文。"""
        try:
            decoded = cast(object, json.loads(payload))
        except json.JSONDecodeError as exc:
            raise CheckpointSchemaError(f"invalid checkpoint JSON: {exc.msg}") from exc
        return self.decode(self._mapping(decoded, "root"))

    def _encode_request(self, request: LoopRequest) -> dict[str, object]:
        return {
            "goal": request.goal,
            "acceptance_criteria": list(request.acceptance_criteria),
            "limits": {
                "max_cycles": request.limits.max_cycles,
                "max_attempts": request.limits.max_attempts,
                "max_steps_per_plan": request.limits.max_steps_per_plan,
                "timeout_seconds": request.limits.timeout_seconds,
            },
            "metadata": self._json_value(request.metadata, "request.metadata"),
        }

    def _decode_request(self, data: Mapping[str, object]) -> LoopRequest:
        limits_data = self._mapping(data.get("limits"), "limits")
        raw_timeout = limits_data.get("timeout_seconds")
        timeout = None if raw_timeout is None else self._number(raw_timeout, "timeout_seconds")
        return LoopRequest(
            goal=self._text(data.get("goal"), "goal"),
            acceptance_criteria=self._string_tuple(
                data.get("acceptance_criteria"), "acceptance_criteria"
            ),
            limits=LoopLimits(
                max_cycles=self._integer(limits_data.get("max_cycles"), "max_cycles"),
                max_attempts=self._integer(limits_data.get("max_attempts"), "max_attempts"),
                max_steps_per_plan=self._integer(
                    limits_data.get("max_steps_per_plan"), "max_steps_per_plan"
                ),
                timeout_seconds=timeout,
            ),
            metadata=self._metadata(data.get("metadata"), "request.metadata"),
        )

    def _encode_interaction(self, interaction: HumanInteractionRequest) -> dict[str, object]:
        return {
            "kind": interaction.kind.value,
            "prompt": interaction.prompt,
            "allowed_actions": [action.value for action in interaction.allowed_actions],
            "interaction_id": interaction.interaction_id,
            "step_id": interaction.step_id,
            "metadata": self._json_value(interaction.metadata, "interaction.metadata"),
            "created_at": interaction.created_at.isoformat(),
        }

    def _decode_interaction(self, data: Mapping[str, object]) -> HumanInteractionRequest:
        raw_step_id = data.get("step_id")
        return HumanInteractionRequest(
            kind=HumanInteractionKind(self._text(data.get("kind"), "interaction.kind")),
            prompt=self._text(data.get("prompt"), "interaction.prompt"),
            allowed_actions=tuple(
                HumanAction(self._text(item, "interaction.allowed_actions"))
                for item in self._sequence(
                    data.get("allowed_actions"), "interaction.allowed_actions"
                )
            ),
            interaction_id=self._text(data.get("interaction_id"), "interaction.interaction_id"),
            step_id=(
                None if raw_step_id is None else self._text(raw_step_id, "interaction.step_id")
            ),
            metadata=self._metadata(data.get("metadata"), "interaction.metadata"),
            created_at=self._datetime(data.get("created_at"), "interaction.created_at"),
        )

    def _encode_response(self, response: HumanResponse) -> dict[str, object]:
        return {
            "interaction_id": response.interaction_id,
            "action": response.action.value,
            "content": response.content,
            "idempotency_key": response.idempotency_key,
            "metadata": self._json_value(response.metadata, "human_response.metadata"),
            "responded_at": response.responded_at.isoformat(),
        }

    def _decode_response(self, data: Mapping[str, object]) -> HumanResponse:
        return HumanResponse(
            interaction_id=self._text(data.get("interaction_id"), "human_response.interaction_id"),
            action=HumanAction(self._text(data.get("action"), "human_response.action")),
            content=self._text(data.get("content"), "human_response.content", allow_empty=True),
            idempotency_key=self._text(
                data.get("idempotency_key"), "human_response.idempotency_key"
            ),
            metadata=self._metadata(data.get("metadata"), "human_response.metadata"),
            responded_at=self._datetime(data.get("responded_at"), "human_response.responded_at"),
        )

    def _encode_human_record(self, record: HumanInteractionRecord) -> dict[str, object]:
        return {
            "request": self._encode_interaction(record.request),
            "response": self._encode_response(record.response),
            "recorded_at": record.recorded_at.isoformat(),
        }

    def _decode_human_record(self, data: Mapping[str, object]) -> HumanInteractionRecord:
        return HumanInteractionRecord(
            request=self._decode_interaction(
                self._mapping(data.get("request"), "human_record.request")
            ),
            response=self._decode_response(
                self._mapping(data.get("response"), "human_record.response")
            ),
            recorded_at=self._datetime(data.get("recorded_at"), "human_record.recorded_at"),
        )

    def _encode_step(self, step: PlanStep) -> dict[str, object]:
        return {
            "description": step.description,
            "executor": step.executor,
            "acceptance_criteria": list(step.acceptance_criteria),
            "requires_approval": step.requires_approval,
            "step_id": step.step_id,
        }

    def _decode_step(self, data: Mapping[str, object]) -> PlanStep:
        requires_approval = data.get("requires_approval")
        if not isinstance(requires_approval, bool):
            raise CheckpointSchemaError("requires_approval must be a boolean")
        return PlanStep(
            description=self._text(data.get("description"), "description"),
            executor=self._text(data.get("executor"), "executor"),
            acceptance_criteria=self._string_tuple(
                data.get("acceptance_criteria"), "acceptance_criteria"
            ),
            requires_approval=requires_approval,
            step_id=self._text(data.get("step_id"), "step_id"),
        )

    def _encode_plan(self, plan: Plan) -> dict[str, object]:
        return {"steps": [self._encode_step(step) for step in plan.steps]}

    def _decode_plan(self, data: Mapping[str, object]) -> Plan:
        return Plan(
            tuple(
                self._decode_step(self._mapping(item, "plan step"))
                for item in self._sequence(data.get("steps"), "steps")
            )
        )

    def _encode_artifact(self, artifact: ArtifactRef) -> dict[str, object]:
        return {
            "name": artifact.name,
            "uri": artifact.uri,
            "media_type": artifact.media_type,
            "metadata": self._json_value(artifact.metadata, "artifact.metadata"),
        }

    def _decode_artifact(self, data: Mapping[str, object]) -> ArtifactRef:
        raw_media_type = data.get("media_type")
        return ArtifactRef(
            name=self._text(data.get("name"), "artifact.name"),
            uri=self._text(data.get("uri"), "artifact.uri"),
            media_type=(
                None
                if raw_media_type is None
                else self._text(raw_media_type, "artifact.media_type")
            ),
            metadata=self._metadata(data.get("metadata"), "artifact.metadata"),
        )

    def _encode_record(self, record: IterationRecord) -> dict[str, object]:
        return {
            "cycle": record.cycle,
            "step_index": record.step_index,
            "step": self._encode_step(record.step),
            "execution": self._encode_execution(record.execution),
            "verification": {
                "passed": record.verification.passed,
                "feedback": record.verification.feedback,
                "score": record.verification.score,
                "evidence": list(record.verification.evidence),
                "failed_criteria": list(record.verification.failed_criteria),
            },
            "attempt": record.attempt,
        }

    def _decode_record(self, data: Mapping[str, object]) -> IterationRecord:
        execution_data = self._mapping(data.get("execution"), "execution")
        verification_data = self._mapping(data.get("verification"), "verification")
        passed = verification_data.get("passed")
        if not isinstance(passed, bool):
            raise CheckpointSchemaError("verification.passed must be a boolean")
        raw_score = verification_data.get("score")
        return IterationRecord(
            cycle=self._integer(data.get("cycle"), "cycle"),
            step_index=self._integer(data.get("step_index"), "step_index"),
            step=self._decode_step(self._mapping(data.get("step"), "step")),
            execution=self._decode_execution(execution_data),
            verification=VerificationResult(
                passed=passed,
                feedback=self._text(
                    verification_data.get("feedback"), "verification.feedback", allow_empty=True
                ),
                score=(
                    None if raw_score is None else self._number(raw_score, "verification.score")
                ),
                evidence=self._string_tuple(
                    verification_data.get("evidence"), "verification.evidence"
                ),
                failed_criteria=self._string_tuple(
                    verification_data.get("failed_criteria"),
                    "verification.failed_criteria",
                ),
            ),
            attempt=self._integer(data.get("attempt"), "attempt"),
        )

    def _encode_execution(self, execution: ExecutionResult) -> dict[str, object]:
        """编码可独立持久化、等待验证的执行结果。"""
        return {
            "output": execution.output,
            "artifacts": [self._encode_artifact(item) for item in execution.artifacts],
            "metadata": self._json_value(execution.metadata, "execution.metadata"),
        }

    def _decode_execution(self, data: Mapping[str, object]) -> ExecutionResult:
        """解码执行结果，并复用记录采用的严格字段校验。"""
        return ExecutionResult(
            output=self._text(data.get("output"), "execution.output", allow_empty=True),
            artifacts=tuple(
                self._decode_artifact(self._mapping(item, "artifact"))
                for item in self._sequence(data.get("artifacts"), "artifacts")
            ),
            metadata=self._metadata(data.get("metadata"), "execution.metadata"),
        )

    def _json_value(self, value: object, path: str) -> object:
        if value is None or isinstance(value, (str, bool, int)):
            return value
        if isinstance(value, float):
            if not math.isfinite(value):
                raise CheckpointSchemaError(f"{path} contains a non-finite number")
            return value
        if isinstance(value, Mapping):
            encoded: dict[str, object] = {}
            for key, item in value.items():
                if not isinstance(key, str):
                    raise CheckpointSchemaError(f"{path} contains a non-string key")
                encoded[key] = self._json_value(item, f"{path}.{key}")
            return encoded
        if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
            return [self._json_value(item, f"{path}[]") for item in value]
        raise CheckpointSchemaError(f"{path} contains unsupported type {type(value).__name__}")

    def _metadata(self, value: object, path: str) -> Mapping[str, object]:
        compatible = self._json_value(value, path)
        return self._mapping(compatible, path)

    @staticmethod
    def _mapping(value: object, field_name: str) -> Mapping[str, object]:
        if not isinstance(value, Mapping):
            raise CheckpointSchemaError(f"{field_name} must be an object")
        result: dict[str, object] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise CheckpointSchemaError(f"{field_name} contains a non-string key")
            result[key] = item
        return result

    @staticmethod
    def _sequence(value: object, field_name: str) -> Sequence[object]:
        if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
            raise CheckpointSchemaError(f"{field_name} must be an array")
        return value

    def _string_tuple(self, value: object, field_name: str) -> tuple[str, ...]:
        return tuple(self._text(item, field_name) for item in self._sequence(value, field_name))

    @staticmethod
    def _text(value: object, field_name: str, *, allow_empty: bool = False) -> str:
        if not isinstance(value, str) or (not allow_empty and not value.strip()):
            suffix = "a string" if allow_empty else "a non-empty string"
            raise CheckpointSchemaError(f"{field_name} must be {suffix}")
        return value

    @staticmethod
    def _integer(value: object, field_name: str) -> int:
        if not isinstance(value, int) or isinstance(value, bool):
            raise CheckpointSchemaError(f"{field_name} must be an integer")
        return value

    @staticmethod
    def _boolean(value: object, field_name: str) -> bool:
        if not isinstance(value, bool):
            raise CheckpointSchemaError(f"{field_name} must be a boolean")
        return value

    @staticmethod
    def _number(value: object, field_name: str) -> float:
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            raise CheckpointSchemaError(f"{field_name} must be a number")
        number = float(value)
        if not math.isfinite(number):
            raise CheckpointSchemaError(f"{field_name} must be finite")
        return number

    def _datetime(self, value: object, field_name: str) -> datetime:
        parsed = datetime.fromisoformat(self._text(value, field_name))
        if parsed.tzinfo is None:
            raise CheckpointSchemaError(f"{field_name} must include a timezone")
        return parsed

    def _optional_datetime(self, value: object, field_name: str) -> datetime | None:
        if value is None:
            return None
        return self._datetime(value, field_name)

    def _optional_text(self, value: object, field_name: str) -> str | None:
        if value is None:
            return None
        return self._text(value, field_name)

    def _optional_integer(self, value: object, field_name: str) -> int | None:
        if value is None:
            return None
        return self._integer(value, field_name)

    def _optional_stop_reason(self, value: object) -> StopReason | None:
        if value is None:
            return None
        return StopReason(self._text(value, "stop_reason"))

    @staticmethod
    def _validate_context(context: LoopContext) -> None:
        if (
            min(
                context.current_step_index,
                context.cycle_count,
                context.total_attempts,
                context.completed_steps,
                context.event_sequence,
                context.revision,
            )
            < 0
        ):
            raise CheckpointSchemaError("checkpoint counters must not be negative")
        if context.active_elapsed_seconds < 0:
            raise CheckpointSchemaError("active_elapsed_seconds must not be negative")
        if context.pending_attempt is not None and context.pending_attempt < 1:
            raise CheckpointSchemaError("pending_attempt must be at least 1")
        if context.pending_execution is not None and (
            context.active_operation_id is None or context.pending_attempt is None
        ):
            raise CheckpointSchemaError(
                "pending execution requires active_operation_id and pending_attempt"
            )
        if context.current_plan is not None and context.current_step_index > len(
            context.current_plan.steps
        ):
            raise CheckpointSchemaError("current_step_index is outside current_plan")
