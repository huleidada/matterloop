"""队列命令和运行记录的版本化 JSON 编解码。"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import replace
from datetime import datetime
from typing import cast

from matterloop_core import (
    CheckpointSchemaError,
    LoopCheckpointCodec,
    LoopContext,
    LoopResult,
    result_from_context,
)
from matterloop_runtime import QueueAction, QueuedRun, RunRecord, RunStatus

from matterloop_integration_redis.errors import RedisPayloadError


class RedisPayloadCodec:
    """把 runtime DTO 编码为不包含 Python 对象的严格 JSON。"""

    schema_version = 1

    def __init__(self) -> None:
        self._checkpoint_codec = LoopCheckpointCodec()

    def dumps_job(self, job: QueuedRun) -> str:
        """序列化队列命令。"""
        try:
            payload: dict[str, object] = {
                "schema_version": self.schema_version,
                "run_id": job.run_id,
                "action": job.action.value,
                "resume_mode": job.resume_mode.value,
                "enqueued_at": job.enqueued_at.isoformat(),
                "request_checkpoint": (
                    None
                    if job.request is None
                    else self._checkpoint_codec.encode(
                        LoopContext(request=job.request, run_id=job.run_id)
                    )
                ),
            }
            return self._dumps(payload)
        except RedisPayloadError:
            raise
        except CheckpointSchemaError as exc:
            raise RedisPayloadError(f"job checkpoint is invalid: {exc}") from exc

    def loads_job(self, value: str | bytes) -> QueuedRun:
        """反序列化并严格校验队列命令。"""
        try:
            payload = self._mapping(json.loads(_text(value)), "job")
            self._check_version(payload)
            run_id = self._string(payload.get("run_id"), "run_id")
            action = QueueAction(self._string(payload.get("action"), "action"))
            request_payload = payload.get("request_checkpoint")
            request = None
            if request_payload is not None:
                request_context = self._checkpoint_codec.decode(
                    self._mapping(request_payload, "request_checkpoint")
                )
                if request_context.run_id != run_id:
                    raise RedisPayloadError("job checkpoint run_id does not match job run_id")
                request = request_context.request
            from matterloop_core import ResumeMode

            return QueuedRun(
                run_id=run_id,
                action=action,
                request=request,
                resume_mode=ResumeMode(self._string(payload.get("resume_mode"), "resume_mode")),
                enqueued_at=self._datetime(payload.get("enqueued_at"), "enqueued_at"),
            )
        except RedisPayloadError:
            raise
        except CheckpointSchemaError as exc:
            raise RedisPayloadError(f"invalid queued-run checkpoint: {exc}") from exc
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise RedisPayloadError(f"invalid queued-run payload: {exc}") from exc

    def dumps_record(self, record: RunRecord) -> str:
        """序列化运行仓储记录和可选结果。"""
        try:
            result_checkpoint = None
            if record.result is not None:
                result = record.result
                if result.run_id != record.run_id:
                    raise RedisPayloadError("result run_id does not match record run_id")
                if result.status.value != record.status.value:
                    raise RedisPayloadError("result status does not match record status")
                result_context = LoopContext(
                    request=record.request,
                    run_id=result.run_id,
                    status=result.status,
                    records=list(result.records),
                    cycle_count=result.cycles,
                    total_attempts=result.total_attempts,
                    completed_steps=result.completed_steps,
                    stop_reason=result.stop_reason,
                    error=result.error,
                    started_at=record.created_at,
                    updated_at=record.updated_at,
                )
                result_checkpoint = self._checkpoint_codec.encode(result_context)
            payload: dict[str, object] = {
                "schema_version": self.schema_version,
                "run_id": record.run_id,
                "request_checkpoint": self._checkpoint_codec.encode(
                    LoopContext(request=record.request, run_id=record.run_id)
                ),
                "status": record.status.value,
                "version": record.version,
                "result_checkpoint": result_checkpoint,
                "result_output": record.result.output if record.result is not None else None,
                "error": record.error,
                "created_at": record.created_at.isoformat(),
                "updated_at": record.updated_at.isoformat(),
            }
            return self._dumps(payload)
        except RedisPayloadError:
            raise
        except CheckpointSchemaError as exc:
            raise RedisPayloadError(f"record checkpoint is invalid: {exc}") from exc

    def loads_record(self, value: str | bytes) -> RunRecord:
        """反序列化并严格校验运行记录。"""
        try:
            payload = self._mapping(json.loads(_text(value)), "record")
            self._check_version(payload)
            run_id = self._string(payload.get("run_id"), "run_id")
            request_context = self._checkpoint_codec.decode(
                self._mapping(payload.get("request_checkpoint"), "request_checkpoint")
            )
            if request_context.run_id != run_id:
                raise RedisPayloadError("request checkpoint run_id does not match record run_id")
            status = RunStatus(self._string(payload.get("status"), "status"))
            raw_result = payload.get("result_checkpoint")
            result: LoopResult | None = None
            if raw_result is not None:
                result_context = self._checkpoint_codec.decode(
                    self._mapping(raw_result, "result_checkpoint")
                )
                result = replace(
                    result_from_context(result_context),
                    output=self._string(
                        payload.get("result_output"),
                        "result_output",
                        allow_empty=True,
                    ),
                )
                if result.run_id != run_id:
                    raise RedisPayloadError("result run_id does not match record run_id")
                if result_context.request != request_context.request:
                    raise RedisPayloadError("result request does not match record request")
                if result.status.value != status.value:
                    raise RedisPayloadError("result status does not match record status")
            return RunRecord(
                run_id=run_id,
                request=request_context.request,
                status=status,
                version=self._integer(payload.get("version"), "version"),
                result=result,
                error=self._string(payload.get("error"), "error", allow_empty=True),
                created_at=self._datetime(payload.get("created_at"), "created_at"),
                updated_at=self._datetime(payload.get("updated_at"), "updated_at"),
            )
        except RedisPayloadError:
            raise
        except CheckpointSchemaError as exc:
            raise RedisPayloadError(f"invalid run-record checkpoint: {exc}") from exc
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise RedisPayloadError(f"invalid run-record payload: {exc}") from exc

    @staticmethod
    def _dumps(payload: Mapping[str, object]) -> str:
        try:
            return json.dumps(
                payload,
                ensure_ascii=False,
                allow_nan=False,
                separators=(",", ":"),
            )
        except (TypeError, ValueError) as exc:
            raise RedisPayloadError(f"payload is not JSON serializable: {exc}") from exc

    def _check_version(self, payload: Mapping[str, object]) -> None:
        version = self._integer(payload.get("schema_version"), "schema_version")
        if version != self.schema_version:
            raise RedisPayloadError(f"unsupported Redis payload schema version: {version}")

    @staticmethod
    def _mapping(value: object, field_name: str) -> Mapping[str, object]:
        if not isinstance(value, Mapping) or not all(isinstance(key, str) for key in value):
            raise RedisPayloadError(f"{field_name} must be an object with string keys")
        return cast(Mapping[str, object], value)

    @staticmethod
    def _string(value: object, field_name: str, *, allow_empty: bool = False) -> str:
        if not isinstance(value, str) or (not allow_empty and not value.strip()):
            raise RedisPayloadError(f"{field_name} must be a string")
        return value

    @staticmethod
    def _integer(value: object, field_name: str) -> int:
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            raise RedisPayloadError(f"{field_name} must be a non-negative integer")
        return value

    def _datetime(self, value: object, field_name: str) -> datetime:
        parsed = datetime.fromisoformat(self._string(value, field_name))
        if parsed.tzinfo is None:
            raise RedisPayloadError(f"{field_name} must include a timezone")
        return parsed


def _text(value: str | bytes | object) -> str:
    """把 Redis 字节响应转换为 UTF-8 文本。"""
    if isinstance(value, bytes):
        try:
            return value.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise RedisPayloadError("Redis payload is not valid UTF-8") from exc
    if isinstance(value, str):
        return value
    raise RedisPayloadError("Redis payload must be text or bytes")
