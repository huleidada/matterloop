"""Redis 运行记录仓储。"""

from __future__ import annotations

from collections.abc import Sequence

from matterloop_runtime import DuplicateRunError, RunRecord

from matterloop_integration_redis.client import AsyncRedisClient, RedisConfig
from matterloop_integration_redis.codec import RedisPayloadCodec
from matterloop_integration_redis.errors import RedisPayloadError

_CREATE_SCRIPT = """
-- matterloop:repository-create
if redis.call('EXISTS', KEYS[1]) == 1 then
  return 0
end
redis.call('SET', KEYS[1], ARGV[1])
redis.call('ZADD', KEYS[2], ARGV[2], ARGV[3])
return 1
"""

_COMPARE_AND_SET_SCRIPT = """
-- matterloop:repository-cas
local current = redis.call('GET', KEYS[1])
if not current then
  return -1
end
local current_ok, decoded = pcall(cjson.decode, current)
local replacement_ok, replacement = pcall(cjson.decode, ARGV[2])
if not current_ok or not replacement_ok or type(decoded) ~= 'table' or type(replacement) ~= 'table' then
  return -2
end
if type(decoded.version) ~= 'number' or type(replacement.version) ~= 'number' then
  return -2
end
if decoded.run_id ~= ARGV[4] or replacement.run_id ~= ARGV[4] then
  return -2
end
if decoded.version ~= tonumber(ARGV[1]) then
  return 0
end
if replacement.version ~= tonumber(ARGV[1]) + 1 then
  return -2
end
if decoded.created_at ~= replacement.created_at then
  return -3
end
redis.call('SET', KEYS[1], ARGV[2])
redis.call('ZADD', KEYS[2], ARGV[3], ARGV[4])
return 1
"""


class RedisRunRepository:
    """使用独立记录 Key 和创建时间索引实现 `RunRepository`。"""

    def __init__(
        self,
        client: AsyncRedisClient,
        config: RedisConfig | None = None,
        *,
        codec: RedisPayloadCodec | None = None,
    ) -> None:
        self._client = client
        self._config = config or RedisConfig()
        self._codec = codec or RedisPayloadCodec()

    async def create(self, record: RunRecord) -> None:
        """原子创建运行记录并加入分页索引。"""
        result = await self._client.eval(
            _CREATE_SCRIPT,
            2,
            self._record_key(record.run_id),
            self._index_key,
            self._codec.dumps_record(record),
            record.created_at.timestamp(),
            record.run_id,
        )
        if _integer_result(result) != 1:
            raise DuplicateRunError(record.run_id)

    async def get(self, run_id: str) -> RunRecord | None:
        """按标识读取运行记录。"""
        value = await self._client.get(self._record_key(run_id))
        if value is None:
            return None
        if not isinstance(value, (str, bytes)):
            raise RedisPayloadError("Redis run record must be text or bytes")
        return self._codec.loads_record(value)

    async def list(self, *, limit: int = 100, offset: int = 0) -> tuple[RunRecord, ...]:
        """按创建时间倒序分页读取运行记录。"""
        if limit < 1 or offset < 0:
            raise ValueError("limit must be positive and offset must not be negative")
        identifiers_value = await self._client.zrevrange(
            self._index_key,
            offset,
            offset + limit - 1,
        )
        identifiers = _text_sequence(identifiers_value)
        if not identifiers:
            return ()
        values = await self._client.mget(
            [self._record_key(identifier) for identifier in identifiers]
        )
        if not isinstance(values, Sequence) or isinstance(values, (str, bytes, bytearray)):
            raise RedisPayloadError("Redis mget response must be an array")
        records: list[RunRecord] = []
        for value in values:
            if value is None:
                continue
            if not isinstance(value, (str, bytes)):
                raise RedisPayloadError("Redis run record must be text or bytes")
            records.append(self._codec.loads_record(value))
        return tuple(records)

    async def compare_and_set(
        self,
        run_id: str,
        expected_version: int,
        replacement: RunRecord,
    ) -> bool:
        """仅在当前版本匹配时原子替换记录。"""
        if expected_version < 0:
            raise ValueError("expected_version must not be negative")
        if replacement.run_id != run_id:
            raise ValueError("replacement run_id must match target run_id")
        if replacement.version != expected_version + 1:
            raise ValueError("replacement version must increment by one")
        result = await self._client.eval(
            _COMPARE_AND_SET_SCRIPT,
            2,
            self._record_key(run_id),
            self._index_key,
            expected_version,
            self._codec.dumps_record(replacement),
            replacement.created_at.timestamp(),
            run_id,
        )
        code = _integer_result(result)
        if code == -2:
            raise RedisPayloadError("Redis run record is corrupted or violates CAS schema")
        if code == -3:
            raise RedisPayloadError("replacement must preserve the original creation time")
        return code == 1

    @property
    def _index_key(self) -> str:
        return f"{self._config.prefix}:runs:index"

    def _record_key(self, run_id: str) -> str:
        return f"{self._config.prefix}:runs:{run_id}"


def _integer_result(value: object) -> int:
    if isinstance(value, bytes):
        try:
            value = value.decode("ascii")
        except UnicodeDecodeError as exc:
            raise RedisPayloadError("Redis integer response is not ASCII") from exc
    if isinstance(value, str):
        try:
            value = int(value)
        except ValueError as exc:
            raise RedisPayloadError("Redis response is not an integer") from exc
    if not isinstance(value, int) or isinstance(value, bool):
        raise RedisPayloadError("Redis response is not an integer")
    return value


def _text_sequence(value: object) -> tuple[str, ...]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        raise RedisPayloadError("Redis sorted-set response must be an array")
    result: list[str] = []
    for item in value:
        if isinstance(item, bytes):
            try:
                item = item.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise RedisPayloadError("Redis identifier is not valid UTF-8") from exc
        if not isinstance(item, str):
            raise RedisPayloadError("Redis identifier must be text or bytes")
        result.append(item)
    return tuple(result)
