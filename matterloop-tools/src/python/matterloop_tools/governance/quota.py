"""按主体维度记账的 MCP 资源配额。"""

from __future__ import annotations

import threading
from dataclasses import dataclass, replace
from math import isfinite

from matterloop_tools.errors import ToolError


class QuotaExceededError(ToolError):
    """配额检查失败，本次调用未被记账。"""

    def __init__(self, key: str, dimension: str, *, tool: str | None = None) -> None:
        """初始化异常。

        Args:
            key: 超限的主体记账键。
            dimension: 超限的资源维度，例如 ``calls`` 或 ``tokens``。
            tool: 超限发生在工具维度时的工具名称。
        """
        scope = key if tool is None else f"{key}/{tool}"
        super().__init__(f"quota exceeded for {scope}: {dimension}")
        self.key = key
        self.dimension = dimension
        self.tool = tool


@dataclass(frozen=True, slots=True)
class QuotaLimits:
    """一个记账桶允许消耗的资源上限。

    Args:
        max_calls: 最大调用次数；``None`` 表示不限制。
        max_tokens: 最大 Token 消耗；``None`` 表示不限制。
        max_cpu_seconds: 最大 CPU 秒数；``None`` 表示不限制。
        max_gpu_seconds: 最大 GPU 秒数；``None`` 表示不限制。
    """

    max_calls: int | None = None
    max_tokens: int | None = None
    max_cpu_seconds: float | None = None
    max_gpu_seconds: float | None = None

    def __post_init__(self) -> None:
        """校验各维度上限均为正数。"""
        if self.max_calls is not None and (type(self.max_calls) is not int or self.max_calls <= 0):
            raise ValueError("max_calls must be a positive integer when provided")
        if self.max_tokens is not None and (
            type(self.max_tokens) is not int or self.max_tokens <= 0
        ):
            raise ValueError("max_tokens must be a positive integer when provided")
        if self.max_cpu_seconds is not None and (
            not isfinite(self.max_cpu_seconds) or self.max_cpu_seconds <= 0
        ):
            raise ValueError("max_cpu_seconds must be a positive finite number when provided")
        if self.max_gpu_seconds is not None and (
            not isfinite(self.max_gpu_seconds) or self.max_gpu_seconds <= 0
        ):
            raise ValueError("max_gpu_seconds must be a positive finite number when provided")


@dataclass(slots=True)
class QuotaUsage:
    """一个记账桶的累计资源消耗。"""

    calls: int = 0
    tokens: int = 0
    cpu_seconds: float = 0.0
    gpu_seconds: float = 0.0


class QuotaTracker:
    """线程安全的配额检查与记账器。

    以主体键（例如 ``tenant:agent``）为主维度、工具名称为可选子维度记账。
    带工具维度的消费同时计入主体桶和工具桶；任一桶超限时整体拒绝且两个
    桶都不落账，保证检查与扣减的原子性。

    Args:
        default_limits: 未显式配置上限的主体桶使用的默认上限；``None``
            表示不限制。工具桶只有显式配置了上限才会参与检查。
    """

    def __init__(self, *, default_limits: QuotaLimits | None = None) -> None:
        self._lock = threading.Lock()
        self._default_limits = default_limits
        self._limits: dict[tuple[str, str | None], QuotaLimits] = {}
        self._usage: dict[tuple[str, str | None], QuotaUsage] = {}

    def set_limits(self, key: str, limits: QuotaLimits, *, tool: str | None = None) -> None:
        """为一个记账桶配置资源上限。

        Args:
            key: 主体记账键。
            limits: 该桶允许的资源上限。
            tool: 配置到工具子维度时的工具名称。
        """
        if not key.strip():
            raise ValueError("quota key must not be empty")
        with self._lock:
            self._limits[(key, tool)] = limits

    def check_and_consume(
        self,
        key: str,
        *,
        tool: str | None = None,
        calls: int = 1,
        tokens: int = 0,
        cpu_seconds: float = 0.0,
        gpu_seconds: float = 0.0,
    ) -> None:
        """原子地检查并扣减一次资源消耗。

        Args:
            key: 主体记账键。
            tool: 同时计入工具子维度时的工具名称。
            calls: 本次消耗的调用次数。
            tokens: 本次消耗的 Token 数。
            cpu_seconds: 本次消耗的 CPU 秒数。
            gpu_seconds: 本次消耗的 GPU 秒数。

        Raises:
            QuotaExceededError: 任一记账桶的任一维度超限；此时不落账。
        """
        if not key.strip():
            raise ValueError("quota key must not be empty")
        if type(calls) is not int or calls < 0:
            raise ValueError("calls must be a non-negative integer")
        if type(tokens) is not int or tokens < 0:
            raise ValueError("tokens must be a non-negative integer")
        if not isfinite(cpu_seconds) or cpu_seconds < 0:
            raise ValueError("cpu_seconds must be a non-negative finite number")
        if not isfinite(gpu_seconds) or gpu_seconds < 0:
            raise ValueError("gpu_seconds must be a non-negative finite number")
        buckets: tuple[tuple[str, str | None], ...] = (
            ((key, None),) if tool is None else ((key, None), (key, tool))
        )
        with self._lock:
            # 先检查全部桶再统一扣减，任何一个维度超限都不能留下部分账目。
            for bucket in buckets:
                limits = self._bucket_limits(bucket)
                if limits is None:
                    continue
                usage = self._usage.get(bucket, QuotaUsage())
                if limits.max_calls is not None and usage.calls + calls > limits.max_calls:
                    raise QuotaExceededError(key, "calls", tool=bucket[1])
                if limits.max_tokens is not None and usage.tokens + tokens > limits.max_tokens:
                    raise QuotaExceededError(key, "tokens", tool=bucket[1])
                if (
                    limits.max_cpu_seconds is not None
                    and usage.cpu_seconds + cpu_seconds > limits.max_cpu_seconds
                ):
                    raise QuotaExceededError(key, "cpu_seconds", tool=bucket[1])
                if (
                    limits.max_gpu_seconds is not None
                    and usage.gpu_seconds + gpu_seconds > limits.max_gpu_seconds
                ):
                    raise QuotaExceededError(key, "gpu_seconds", tool=bucket[1])
            for bucket in buckets:
                usage = self._usage.setdefault(bucket, QuotaUsage())
                usage.calls += calls
                usage.tokens += tokens
                usage.cpu_seconds += cpu_seconds
                usage.gpu_seconds += gpu_seconds

    def usage(self, key: str, *, tool: str | None = None) -> QuotaUsage:
        """返回一个记账桶的当前消耗快照。

        Args:
            key: 主体记账键。
            tool: 查询工具子维度时的工具名称。

        Returns:
            独立副本，修改不影响内部账目。
        """
        with self._lock:
            usage = self._usage.get((key, tool))
            return QuotaUsage() if usage is None else replace(usage)

    def reset(self, key: str, *, tool: str | None = None) -> None:
        """清空一个主体的账目。

        Args:
            key: 主体记账键。
            tool: 只清空指定工具子维度时的工具名称；``None`` 时清空该
                主体的全部桶。
        """
        with self._lock:
            if tool is not None:
                self._usage.pop((key, tool), None)
                return
            for bucket in [bucket for bucket in self._usage if bucket[0] == key]:
                del self._usage[bucket]

    def _bucket_limits(self, bucket: tuple[str, str | None]) -> QuotaLimits | None:
        """返回记账桶生效的上限；主体桶回退到默认上限。"""
        limits = self._limits.get(bucket)
        if limits is None and bucket[1] is None:
            return self._default_limits
        return limits
