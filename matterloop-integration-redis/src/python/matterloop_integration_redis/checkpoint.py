"""使用 Redis CAS 持久化 Core Loop 检查点。"""

from __future__ import annotations

from matterloop_core import (
    CheckpointConflictError,
    CheckpointSchemaError,
    LoopCheckpointCodec,
    LoopContext,
)

from matterloop_integration_redis.client import AsyncRedisClient, RedisConfig
from matterloop_integration_redis.errors import RedisPayloadError

_SAVE_SCRIPT = """
-- matterloop:checkpoint-save
local expected_revision = tonumber(ARGV[1])
if not expected_revision then
  return -2
end

local replacement_ok, replacement = pcall(cjson.decode, ARGV[2])
if not replacement_ok or type(replacement) ~= 'table' or type(replacement.context) ~= 'table' then
  return -2
end
if replacement.context.run_id ~= ARGV[3]
    or type(replacement.context.revision) ~= 'number'
    or replacement.context.revision % 1 ~= 0
    or replacement.context.revision ~= expected_revision + 1 then
  return -2
end

local current = redis.call('GET', KEYS[1])
if not current then
  if expected_revision ~= 0 then
    return 0
  end
  redis.call('SET', KEYS[1], ARGV[2])
  return 1
end

local current_ok, decoded = pcall(cjson.decode, current)
if not current_ok or type(decoded) ~= 'table' or type(decoded.context) ~= 'table' then
  return -2
end
if decoded.context.run_id ~= ARGV[3]
    or type(decoded.context.revision) ~= 'number'
    or decoded.context.revision % 1 ~= 0 then
  return -2
end
if decoded.context.revision ~= expected_revision then
  return 0
end

redis.call('SET', KEYS[1], ARGV[2])
return expected_revision + 1
"""


class RedisCheckpointStore:
    """把版本化 Loop 快照保存到宿主显式注入的 Redis 客户端。

    Args:
        client: 已由宿主配置连接、认证、TLS 和超时的异步 Redis 客户端。
        config: 非敏感 Redis key 前缀配置。
        codec: 可选 Core 检查点编解码器。
    """

    def __init__(
        self,
        client: AsyncRedisClient,
        config: RedisConfig | None = None,
        *,
        codec: LoopCheckpointCodec | None = None,
    ) -> None:
        self._client = client
        self._config = config or RedisConfig()
        self._codec = codec or LoopCheckpointCodec()

    async def save(
        self,
        context: LoopContext,
        *,
        expected_revision: int | None = None,
    ) -> int:
        """使用 revision CAS 原子创建或更新检查点。

        Args:
            context: 需要持久化的隔离 Loop 上下文。
            expected_revision: 调用方读取到的 revision；省略时使用上下文 revision。

        Returns:
            成功提交后的新 revision。

        Raises:
            CheckpointConflictError: Redis 中的 revision 与调用方预期不一致。
            RedisPayloadError: 检查点无法编码，或 Redis 中的现有数据损坏。
            ValueError: ``run_id`` 为空，或 ``expected_revision`` 不是非负整数。
        """
        if not context.run_id.strip():
            raise ValueError("run_id must not be empty")
        expected = context.revision if expected_revision is None else expected_revision
        if not isinstance(expected, int) or isinstance(expected, bool) or expected < 0:
            raise ValueError("expected_revision must be a non-negative integer")

        replacement = context.snapshot()
        replacement.revision = expected + 1
        try:
            payload = self._codec.dumps(replacement)
        except CheckpointSchemaError as exc:
            raise RedisPayloadError(f"checkpoint is invalid: {exc}") from exc

        result = await self._client.eval(
            _SAVE_SCRIPT,
            1,
            self._checkpoint_key(context.run_id),
            str(expected),
            payload,
            context.run_id,
        )
        revision = _integer_result(result)
        if revision == 0:
            raise CheckpointConflictError(
                f"checkpoint revision conflict for {context.run_id}: expected {expected}"
            )
        if revision == -2:
            raise RedisPayloadError("Redis checkpoint is corrupted or violates CAS schema")
        if revision != expected + 1:
            raise RedisPayloadError(f"Redis returned an invalid checkpoint revision: {revision}")
        return revision

    async def load(self, run_id: str) -> LoopContext | None:
        """读取并严格校验指定运行的检查点。

        Args:
            run_id: 需要恢复的稳定运行标识。

        Returns:
            解码后的隔离上下文；key 不存在时返回 ``None``。

        Raises:
            RedisPayloadError: Redis 返回值不是文本、UTF-8 无效或检查点损坏。
            ValueError: ``run_id`` 为空。
        """
        if not run_id.strip():
            raise ValueError("run_id must not be empty")
        value = await self._client.get(self._checkpoint_key(run_id))
        if value is None:
            return None
        payload = _checkpoint_text(value)
        try:
            context = self._codec.loads(payload)
        except CheckpointSchemaError as exc:
            raise RedisPayloadError(f"Redis checkpoint is invalid: {exc}") from exc
        if context.run_id != run_id:
            raise RedisPayloadError("Redis checkpoint run_id does not match its key")
        return context

    def _checkpoint_key(self, run_id: str) -> str:
        return f"{self._config.prefix}:checkpoints:{run_id}"


def _checkpoint_text(value: object) -> str:
    if isinstance(value, bytes):
        try:
            return value.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise RedisPayloadError("Redis checkpoint is not valid UTF-8") from exc
    if isinstance(value, str):
        return value
    raise RedisPayloadError("Redis checkpoint must be text or bytes")


def _integer_result(value: object) -> int:
    if isinstance(value, bytes):
        try:
            value = value.decode("ascii")
        except UnicodeDecodeError as exc:
            raise RedisPayloadError("Redis checkpoint CAS response is not ASCII") from exc
    if isinstance(value, str):
        try:
            value = int(value)
        except ValueError as exc:
            raise RedisPayloadError("Redis checkpoint CAS response is not an integer") from exc
    if not isinstance(value, int) or isinstance(value, bool):
        raise RedisPayloadError("Redis checkpoint CAS response is not an integer")
    return value


__all__ = ["RedisCheckpointStore"]
