"""带指数退避和抖动的异常重试策略。"""

import random
from dataclasses import dataclass

from matterloop_core import LoopContext, RetryAction, RetryDecision


@dataclass(frozen=True, slots=True)
class RetryConfig:
    """配置重试次数与退避参数。"""

    max_attempts: int = 3
    base_delay_seconds: float = 0.5
    max_delay_seconds: float = 30
    jitter_ratio: float = 0.2

    def __post_init__(self) -> None:
        """校验退避参数范围。"""
        if self.max_attempts < 1:
            raise ValueError("max_attempts must be at least 1")
        if min(self.base_delay_seconds, self.max_delay_seconds) < 0:
            raise ValueError("retry delays must not be negative")
        if not 0 <= self.jitter_ratio <= 1:
            raise ValueError("jitter_ratio must be between 0 and 1")


class ExponentialBackoffRetryPolicy:
    """对指定异常执行有上限的指数退避重试。"""

    def __init__(
        self,
        config: RetryConfig | None = None,
        retryable: tuple[type[Exception], ...] = (TimeoutError, ConnectionError),
        random_source: random.Random | None = None,
    ) -> None:
        self._config = config or RetryConfig()
        self._retryable = retryable
        self._random = random_source or random.Random()

    def decide(self, error: Exception, attempt: int, context: LoopContext) -> RetryDecision:
        """返回重试或立即失败决策。"""
        del context
        if not isinstance(error, self._retryable) or attempt >= self._config.max_attempts:
            return RetryDecision(RetryAction.FAIL)
        base = min(
            self._config.max_delay_seconds,
            self._config.base_delay_seconds * (2 ** (attempt - 1)),
        )
        jitter = base * self._config.jitter_ratio * self._random.uniform(-1, 1)
        return RetryDecision(RetryAction.RETRY, max(0, base + jitter))
