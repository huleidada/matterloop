"""Redis 客户端协议与非敏感适配器配置。"""

from __future__ import annotations

import math
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

_KEY_PREFIX = re.compile(r"^[A-Za-z0-9_.:{}-]+$")


@dataclass(frozen=True, slots=True)
class RedisConfig:
    """保存 Redis 集成的非敏感配置。

    Args:
        prefix: 所有 Redis Key 使用的隔离前缀。
        lease_seconds: 队列租约的建议有效秒数。
        event_max_length: 每个运行最多保留的近似事件数量。

    Note:
        该配置不包含 URL、主机、用户名、密码或环境变量名。Redis 客户端必须由宿主
        应用显式构造并注入各适配器。
    """

    prefix: str = "matterloop"
    lease_seconds: float = 60.0
    event_max_length: int = 10_000

    def __post_init__(self) -> None:
        """校验配置边界，防止把连接信息误传到 Key 前缀。"""
        if not _KEY_PREFIX.fullmatch(self.prefix):
            raise ValueError("prefix contains characters outside the Redis key-prefix allowlist")
        if (
            not isinstance(self.lease_seconds, (int, float))
            or isinstance(self.lease_seconds, bool)
            or not math.isfinite(self.lease_seconds)
            or self.lease_seconds <= 0
        ):
            raise ValueError("lease_seconds must be greater than 0")
        if (
            not isinstance(self.event_max_length, int)
            or isinstance(self.event_max_length, bool)
            or self.event_max_length < 1
        ):
            raise ValueError("event_max_length must be at least 1")


@runtime_checkable
class AsyncRedisClient(Protocol):
    """适配器使用的最小异步 Redis 客户端协议。"""

    async def eval(self, script: str, numkeys: int, *keys_and_args: object) -> object:
        """原子执行 Lua 脚本。"""
        ...

    async def get(self, name: str) -> object:
        """读取字符串值。"""
        ...

    async def mget(self, keys: Sequence[str]) -> object:
        """批量读取字符串值。"""
        ...

    async def zrevrange(self, name: str, start: int, end: int) -> object:
        """倒序读取有序集合成员。"""
        ...

    async def xadd(
        self,
        name: str,
        fields: Mapping[str, str],
        *,
        maxlen: int,
        approximate: bool,
    ) -> object:
        """向事件流追加记录。"""
        ...

    async def xrange(
        self,
        name: str,
        *,
        min: str,
        max: str,
        count: int,
    ) -> object:
        """读取事件流区间。"""
        ...

    async def aclose(self) -> None:
        """关闭客户端连接池。"""
        ...
