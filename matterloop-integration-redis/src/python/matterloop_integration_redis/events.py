"""Redis Stream 生命周期事件发布与读取。"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import cast

from matterloop_core import CheckpointSchemaError, LoopCheckpointCodec, LoopEvent, LoopEventType

from matterloop_integration_redis.client import AsyncRedisClient, RedisConfig
from matterloop_integration_redis.errors import RedisPayloadError

_STREAM_ID = re.compile(r"^[0-9]+-[0-9]+$")


class RedisEventPublisher:
    """把版本化 Loop 事件写入按运行隔离的 Redis Stream。

    同一实例也实现 runtime 的 `RunEventReader`，可直接传给 `QueueRuntime` 提供事件分页。
    """

    schema_version = 1

    def __init__(
        self,
        client: AsyncRedisClient,
        config: RedisConfig | None = None,
        *,
        checkpoint_codec: LoopCheckpointCodec | None = None,
    ) -> None:
        self._client = client
        self._config = config or RedisConfig()
        self._checkpoint_codec = checkpoint_codec or LoopCheckpointCodec()

    async def publish(self, event: LoopEvent) -> None:
        """向运行事件流追加一个不可变事件快照。"""
        try:
            payload = {
                "schema_version": self.schema_version,
                "event_type": event.event_type.value,
                "run_id": event.context.run_id,
                "occurred_at": event.occurred_at.isoformat(),
                "detail": event.detail,
                "checkpoint": self._checkpoint_codec.encode(event.context),
            }
            encoded = json.dumps(
                payload,
                ensure_ascii=False,
                allow_nan=False,
                separators=(",", ":"),
            )
        except (CheckpointSchemaError, TypeError, ValueError) as exc:
            raise RedisPayloadError(f"event is not JSON serializable: {exc}") from exc
        await self._client.xadd(
            self._event_key(event.context.run_id),
            {"payload": encoded},
            maxlen=self._config.event_max_length,
            approximate=True,
        )

    async def list_events(
        self,
        run_id: str,
        *,
        after: str | None = None,
        limit: int = 100,
    ) -> tuple[Mapping[str, object], ...]:
        """按 Redis Stream ID 正序读取事件。"""
        if not run_id.strip():
            raise ValueError("run_id must not be empty")
        if limit < 1:
            raise ValueError("limit must be at least 1")
        if after is not None and not _STREAM_ID.fullmatch(after):
            raise ValueError("after must be a Redis Stream ID such as '123-0'")
        minimum = "-" if after is None else f"({after}"
        raw_entries = await self._client.xrange(
            self._event_key(run_id),
            min=minimum,
            max="+",
            count=limit,
        )
        if not isinstance(raw_entries, Sequence) or isinstance(
            raw_entries, (str, bytes, bytearray)
        ):
            raise RedisPayloadError("Redis stream response must be an array")
        events: list[Mapping[str, object]] = []
        for entry in raw_entries:
            identifier, fields = _stream_entry(entry)
            raw_payload = fields.get("payload")
            if raw_payload is None:
                raise RedisPayloadError("Redis stream entry has no payload")
            try:
                payload = json.loads(_text(raw_payload))
            except json.JSONDecodeError as exc:
                raise RedisPayloadError("Redis event payload is invalid JSON") from exc
            if not isinstance(payload, Mapping) or not all(isinstance(key, str) for key in payload):
                raise RedisPayloadError("Redis event payload must be an object")
            typed_payload = cast(Mapping[str, object], payload)
            self._validate_payload(typed_payload, expected_run_id=run_id)
            # Redis Stream ID 是可信分页游标，不能被损坏载荷中的同名字段覆盖。
            events.append({**typed_payload, "event_id": identifier})
        return tuple(events)

    def _event_key(self, run_id: str) -> str:
        return f"{self._config.prefix}:events:{run_id}"

    def _validate_payload(
        self,
        payload: Mapping[str, object],
        *,
        expected_run_id: str,
    ) -> None:
        version = payload.get("schema_version")
        if (
            not isinstance(version, int)
            or isinstance(version, bool)
            or version != self.schema_version
        ):
            raise RedisPayloadError(f"unsupported event schema version: {version}")
        run_id = payload.get("run_id")
        if not isinstance(run_id, str) or run_id != expected_run_id:
            raise RedisPayloadError("event run_id does not match requested event stream")
        event_type = payload.get("event_type")
        if not isinstance(event_type, str):
            raise RedisPayloadError("event_type must be a string")
        try:
            LoopEventType(event_type)
        except ValueError as exc:
            raise RedisPayloadError(f"unsupported event type: {event_type}") from exc
        occurred_at = payload.get("occurred_at")
        if not isinstance(occurred_at, str):
            raise RedisPayloadError("occurred_at must be a string")
        try:
            parsed_time = datetime.fromisoformat(occurred_at)
        except ValueError as exc:
            raise RedisPayloadError("occurred_at is not an ISO datetime") from exc
        if parsed_time.tzinfo is None:
            raise RedisPayloadError("occurred_at must include a timezone")
        if not isinstance(payload.get("detail"), str):
            raise RedisPayloadError("event detail must be a string")
        checkpoint = payload.get("checkpoint")
        if not isinstance(checkpoint, Mapping):
            raise RedisPayloadError("event checkpoint must be an object")
        try:
            context = self._checkpoint_codec.decode(cast(Mapping[str, object], checkpoint))
        except CheckpointSchemaError as exc:
            raise RedisPayloadError(f"event checkpoint is invalid: {exc}") from exc
        if context.run_id != expected_run_id:
            raise RedisPayloadError("event checkpoint run_id does not match event stream")


def _stream_entry(value: object) -> tuple[str, Mapping[str, object]]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        raise RedisPayloadError("Redis stream entry must be an array")
    if len(value) != 2:
        raise RedisPayloadError("Redis stream entry must contain id and fields")
    identifier = _text(value[0])
    raw_fields = value[1]
    if not isinstance(raw_fields, Mapping):
        raise RedisPayloadError("Redis stream fields must be an object")
    fields: dict[str, object] = {}
    for key, item in raw_fields.items():
        fields[_text(key)] = item
    return identifier, fields


def _text(value: object) -> str:
    if isinstance(value, bytes):
        try:
            return value.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise RedisPayloadError("Redis value is not valid UTF-8") from exc
    if isinstance(value, str):
        return value
    raise RedisPayloadError("Redis value must be text or bytes")
