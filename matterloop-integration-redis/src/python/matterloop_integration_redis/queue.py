"""基于 Redis Lua 原子操作的租约队列后端。"""

from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from matterloop_runtime import DuplicateRunError, QueuedRun, QueueLease

from matterloop_integration_redis.client import AsyncRedisClient, RedisConfig
from matterloop_integration_redis.codec import RedisPayloadCodec
from matterloop_integration_redis.errors import RedisPayloadError

_ENQUEUE_SCRIPT = """
-- matterloop:enqueue
if redis.call('HEXISTS', KEYS[1], ARGV[1]) == 1 then
  return 0
end
redis.call('HSET', KEYS[1], ARGV[1], ARGV[2])
redis.call('HSET', KEYS[2], ARGV[1], 1)
redis.call('SREM', KEYS[4], ARGV[1])
redis.call('RPUSH', KEYS[3], ARGV[1])
return 1
"""

_LEASE_SCRIPT = """
-- matterloop:lease
local now = tonumber(ARGV[1])
local due = redis.call('ZRANGEBYSCORE', KEYS[2], '-inf', now)
for _, run_id in ipairs(due) do
  redis.call('ZREM', KEYS[2], run_id)
  redis.call('RPUSH', KEYS[1], run_id)
end

local expired = redis.call('ZRANGEBYSCORE', KEYS[6], '-inf', now)
for _, expired_lease_id in ipairs(expired) do
  local raw_lease = redis.call('HGET', KEYS[5], expired_lease_id)
  redis.call('HDEL', KEYS[5], expired_lease_id)
  redis.call('ZREM', KEYS[6], expired_lease_id)
  if raw_lease then
    local lease = cjson.decode(raw_lease)
    redis.call('HDEL', KEYS[7], lease.run_id)
    if redis.call('SISMEMBER', KEYS[8], lease.run_id) == 1 then
      redis.call('HDEL', KEYS[3], lease.run_id)
      redis.call('HDEL', KEYS[4], lease.run_id)
      redis.call('SREM', KEYS[8], lease.run_id)
    else
      redis.call('HINCRBY', KEYS[4], lease.run_id, 1)
      redis.call('LPUSH', KEYS[1], lease.run_id)
    end
  end
end

while true do
  local run_id = redis.call('LPOP', KEYS[1])
  if not run_id then
    return nil
  end
  if redis.call('SISMEMBER', KEYS[8], run_id) == 1 then
    redis.call('HDEL', KEYS[3], run_id)
    redis.call('HDEL', KEYS[4], run_id)
    redis.call('SREM', KEYS[8], run_id)
  else
    local job = redis.call('HGET', KEYS[3], run_id)
    if job then
      local attempt = tonumber(redis.call('HGET', KEYS[4], run_id) or '1')
      local lease = cjson.encode({
        run_id = run_id,
        worker_id = ARGV[3],
        expires_at = tonumber(ARGV[4])
      })
      redis.call('HSET', KEYS[5], ARGV[2], lease)
      redis.call('ZADD', KEYS[6], ARGV[4], ARGV[2])
      redis.call('HSET', KEYS[7], run_id, ARGV[2])
      return {job, tostring(attempt)}
    end
  end
end
"""

_ACKNOWLEDGE_SCRIPT = """
-- matterloop:acknowledge
local raw_lease = redis.call('HGET', KEYS[1], ARGV[1])
if not raw_lease then
  return 0
end
local lease = cjson.decode(raw_lease)
if lease.run_id ~= ARGV[2] then
  return 0
end
redis.call('HDEL', KEYS[1], ARGV[1])
redis.call('ZREM', KEYS[2], ARGV[1])
redis.call('HDEL', KEYS[3], ARGV[2])
redis.call('HDEL', KEYS[4], ARGV[2])
redis.call('HDEL', KEYS[5], ARGV[2])
redis.call('SREM', KEYS[6], ARGV[2])
return 1
"""

_RELEASE_SCRIPT = """
-- matterloop:release
local raw_lease = redis.call('HGET', KEYS[1], ARGV[1])
if not raw_lease then
  return 0
end
local lease = cjson.decode(raw_lease)
if lease.run_id ~= ARGV[2] then
  return 0
end
redis.call('HDEL', KEYS[1], ARGV[1])
redis.call('ZREM', KEYS[2], ARGV[1])
redis.call('HDEL', KEYS[3], ARGV[2])
if redis.call('SISMEMBER', KEYS[7], ARGV[2]) == 1 then
  redis.call('HDEL', KEYS[4], ARGV[2])
  redis.call('HDEL', KEYS[5], ARGV[2])
  redis.call('SREM', KEYS[7], ARGV[2])
  return 1
end
redis.call('HINCRBY', KEYS[5], ARGV[2], 1)
if tonumber(ARGV[3]) > tonumber(ARGV[4]) then
  redis.call('ZADD', KEYS[6], ARGV[3], ARGV[2])
else
  redis.call('RPUSH', KEYS[8], ARGV[2])
end
return 1
"""

_CANCEL_SCRIPT = """
-- matterloop:cancel
if redis.call('HEXISTS', KEYS[1], ARGV[1]) == 0 then
  return 0
end
redis.call('SADD', KEYS[6], ARGV[1])
redis.call('LREM', KEYS[3], 0, ARGV[1])
redis.call('ZREM', KEYS[4], ARGV[1])
if redis.call('HEXISTS', KEYS[5], ARGV[1]) == 0 then
  redis.call('HDEL', KEYS[1], ARGV[1])
  redis.call('HDEL', KEYS[2], ARGV[1])
  redis.call('SREM', KEYS[6], ARGV[1])
end
return 1
"""


class RedisQueueBackend:
    """实现 `QueueBackend` 的 Redis 租约队列。

    所有多 Key 状态转换都在 Lua 中原子完成。连接对象由调用方注入，配置只保存 Key
    前缀和默认租约等非敏感信息。
    """

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

    async def enqueue(self, job: QueuedRun) -> None:
        """原子加入一条新命令，拒绝尚未完成的重复运行标识。"""
        result = await self._client.eval(
            _ENQUEUE_SCRIPT,
            4,
            self._jobs_key,
            self._attempts_key,
            self._pending_key,
            self._cancelled_key,
            job.run_id,
            self._codec.dumps_job(job),
        )
        if _integer_result(result) != 1:
            raise DuplicateRunError(job.run_id)

    async def lease(self, worker_id: str, lease_seconds: float | None = None) -> QueueLease | None:
        """租用一条命令，同时回收过期租约和已到期延迟命令。"""
        if not worker_id.strip():
            raise ValueError("worker_id must not be empty")
        duration = self._config.lease_seconds if lease_seconds is None else lease_seconds
        if not math.isfinite(duration) or duration <= 0:
            raise ValueError("lease_seconds must be greater than 0")
        now = datetime.now(timezone.utc)
        expires_at = now + timedelta(seconds=duration)
        lease_id = uuid4().hex
        result = await self._client.eval(
            _LEASE_SCRIPT,
            8,
            self._pending_key,
            self._delayed_key,
            self._jobs_key,
            self._attempts_key,
            self._leases_key,
            self._lease_expiry_key,
            self._run_leases_key,
            self._cancelled_key,
            now.timestamp(),
            lease_id,
            worker_id,
            expires_at.timestamp(),
        )
        if result is None:
            return None
        values = _sequence_result(result)
        if len(values) != 2:
            raise RedisPayloadError("Redis lease response must contain job and attempt")
        job_payload = values[0]
        if not isinstance(job_payload, (str, bytes)):
            raise RedisPayloadError("Redis lease job must be text or bytes")
        return QueueLease(
            lease_id=lease_id,
            job=self._codec.loads_job(job_payload),
            worker_id=worker_id,
            expires_at=expires_at,
            attempt=_integer_result(values[1]),
        )

    async def acknowledge(self, lease: QueueLease) -> None:
        """确认有效租约并清理命令数据；重复确认是安全空操作。"""
        await self._client.eval(
            _ACKNOWLEDGE_SCRIPT,
            6,
            self._leases_key,
            self._lease_expiry_key,
            self._run_leases_key,
            self._jobs_key,
            self._attempts_key,
            self._cancelled_key,
            lease.lease_id,
            lease.job.run_id,
        )

    async def release(self, lease: QueueLease, *, delay_seconds: float = 0) -> None:
        """释放有效租约，并按可选延迟重新排队。"""
        if not math.isfinite(delay_seconds) or delay_seconds < 0:
            raise ValueError("delay_seconds must not be negative")
        now = datetime.now(timezone.utc).timestamp()
        await self._client.eval(
            _RELEASE_SCRIPT,
            8,
            self._leases_key,
            self._lease_expiry_key,
            self._run_leases_key,
            self._jobs_key,
            self._attempts_key,
            self._delayed_key,
            self._cancelled_key,
            self._pending_key,
            lease.lease_id,
            lease.job.run_id,
            now + delay_seconds,
            now,
        )

    async def cancel(self, run_id: str) -> bool:
        """取消等待中的命令，并标记已租用命令在释放时清理。"""
        result = await self._client.eval(
            _CANCEL_SCRIPT,
            6,
            self._jobs_key,
            self._attempts_key,
            self._pending_key,
            self._delayed_key,
            self._run_leases_key,
            self._cancelled_key,
            run_id,
        )
        return _integer_result(result) == 1

    @property
    def _pending_key(self) -> str:
        return f"{self._config.prefix}:queue:pending"

    @property
    def _delayed_key(self) -> str:
        return f"{self._config.prefix}:queue:delayed"

    @property
    def _jobs_key(self) -> str:
        return f"{self._config.prefix}:queue:jobs"

    @property
    def _attempts_key(self) -> str:
        return f"{self._config.prefix}:queue:attempts"

    @property
    def _leases_key(self) -> str:
        return f"{self._config.prefix}:queue:leases"

    @property
    def _lease_expiry_key(self) -> str:
        return f"{self._config.prefix}:queue:lease-expiry"

    @property
    def _run_leases_key(self) -> str:
        return f"{self._config.prefix}:queue:run-leases"

    @property
    def _cancelled_key(self) -> str:
        return f"{self._config.prefix}:queue:cancelled"


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


def _sequence_result(value: object) -> tuple[object, ...]:
    if not isinstance(value, (list, tuple)):
        raise RedisPayloadError("Redis response is not an array")
    return tuple(value)
