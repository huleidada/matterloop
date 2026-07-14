"""把 LoopRequest 编解码为严格、版本化的 JSON DTO。"""

from __future__ import annotations

import json
import math
from collections.abc import Mapping
from typing import cast

from matterloop_core import LoopLimits, LoopRequest

from matterloop_integration_celery.errors import CeleryPayloadError


class CeleryMessageCodec:
    """在 Celery 消息边界序列化和校验 Loop 请求。"""

    schema_version = 1

    def encode_request(self, request: LoopRequest) -> dict[str, object]:
        """把 Loop 请求转换为只含 JSON 数据的 DTO。

        Args:
            request: 需要发送给 Worker 的 Loop 请求。

        Returns:
            不包含 Runtime 或组件实例的普通字典。

        Raises:
            CeleryPayloadError: 请求元数据不能由 JSON 安全序列化。
        """
        metadata = self._normalize_json_mapping(request.metadata, "metadata")
        timeout_seconds = request.limits.timeout_seconds
        if timeout_seconds is not None and not math.isfinite(timeout_seconds):
            raise CeleryPayloadError("timeout_seconds must be finite")
        return {
            "schema_version": self.schema_version,
            "goal": request.goal,
            "acceptance_criteria": list(request.acceptance_criteria),
            "limits": {
                "max_cycles": request.limits.max_cycles,
                "max_attempts": request.limits.max_attempts,
                "max_steps_per_plan": request.limits.max_steps_per_plan,
                "timeout_seconds": timeout_seconds,
            },
            "metadata": metadata,
        }

    def decode_request(self, payload: Mapping[str, object]) -> LoopRequest:
        """严格校验 DTO 并恢复 Loop 请求。

        Args:
            payload: Celery JSON 解码后的请求对象。

        Returns:
            经过领域校验的 Loop 请求。

        Raises:
            CeleryPayloadError: Schema 版本、字段类型或字段集合无效。
        """
        expected_fields = {
            "schema_version",
            "goal",
            "acceptance_criteria",
            "limits",
            "metadata",
        }
        if set(payload) != expected_fields:
            raise CeleryPayloadError("request payload fields do not match schema")
        version = self._integer(payload.get("schema_version"), "schema_version")
        if version != self.schema_version:
            raise CeleryPayloadError(f"unsupported Celery payload schema version: {version}")
        goal = self._string(payload.get("goal"), "goal")
        criteria_value = payload.get("acceptance_criteria")
        if not isinstance(criteria_value, list) or not all(
            isinstance(item, str) and bool(item.strip()) for item in criteria_value
        ):
            raise CeleryPayloadError("acceptance_criteria must contain non-empty strings")
        limits_value = self._mapping(payload.get("limits"), "limits")
        if set(limits_value) != {
            "max_cycles",
            "max_attempts",
            "max_steps_per_plan",
            "timeout_seconds",
        }:
            raise CeleryPayloadError("limits fields do not match schema")
        timeout_value = limits_value.get("timeout_seconds")
        if timeout_value is not None and (
            isinstance(timeout_value, bool) or not isinstance(timeout_value, (int, float))
        ):
            raise CeleryPayloadError("timeout_seconds must be a number or null")
        if timeout_value is not None and not math.isfinite(float(timeout_value)):
            raise CeleryPayloadError("timeout_seconds must be finite")
        metadata = self._normalize_json_mapping(
            self._mapping(payload.get("metadata"), "metadata"),
            "metadata",
        )
        try:
            return LoopRequest(
                goal=goal,
                acceptance_criteria=tuple(cast(list[str], criteria_value)),
                limits=LoopLimits(
                    max_cycles=self._integer(limits_value.get("max_cycles"), "max_cycles"),
                    max_attempts=self._integer(limits_value.get("max_attempts"), "max_attempts"),
                    max_steps_per_plan=self._integer(
                        limits_value.get("max_steps_per_plan"), "max_steps_per_plan"
                    ),
                    timeout_seconds=None if timeout_value is None else float(timeout_value),
                ),
                metadata=metadata,
            )
        except ValueError as exc:
            raise CeleryPayloadError(f"request payload violates domain rules: {exc}") from exc

    @staticmethod
    def _normalize_json_mapping(
        value: Mapping[str, object],
        field_name: str,
    ) -> dict[str, object]:
        try:
            encoded = json.dumps(
                dict(value),
                ensure_ascii=False,
                allow_nan=False,
                separators=(",", ":"),
            )
            decoded = json.loads(encoded)
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise CeleryPayloadError(f"{field_name} must contain only JSON values") from exc
        if not isinstance(decoded, dict) or not all(isinstance(key, str) for key in decoded):
            raise CeleryPayloadError(f"{field_name} must be an object with string keys")
        return cast(dict[str, object], decoded)

    @staticmethod
    def _mapping(value: object, field_name: str) -> Mapping[str, object]:
        if not isinstance(value, Mapping) or not all(isinstance(key, str) for key in value):
            raise CeleryPayloadError(f"{field_name} must be an object with string keys")
        return cast(Mapping[str, object], value)

    @staticmethod
    def _string(value: object, field_name: str) -> str:
        if not isinstance(value, str) or not value.strip():
            raise CeleryPayloadError(f"{field_name} must be a non-empty string")
        return value

    @staticmethod
    def _integer(value: object, field_name: str) -> int:
        if not isinstance(value, int) or isinstance(value, bool) or value < 1:
            raise CeleryPayloadError(f"{field_name} must be a positive integer")
        return value
