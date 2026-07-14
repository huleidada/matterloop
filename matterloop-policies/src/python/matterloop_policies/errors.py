"""定义计算额度模块可分类处理的异常。"""

from __future__ import annotations

from matterloop_core import ResourceLimitExceededError as CoreResourceLimitExceededError


class BudgetError(Exception):
    """所有本地计算额度异常的基类。"""


class BudgetConfigurationError(BudgetError):
    """预算配置无法提供承诺的强制边界。"""


class ResourceLimitExceededError(BudgetError, CoreResourceLimitExceededError):
    """一次原子额度申请将超过某个 scope 的硬上限。

    异常只包含资源名称和数值，不拼接模型请求、供应商异常或凭据。
    """

    def __init__(
        self,
        *,
        scope: str,
        resource: str,
        limit: int,
        current: int,
        requested: int,
    ) -> None:
        self.scope = scope
        self.resource = resource
        self.limit = limit
        self.current = current
        self.requested = requested
        super().__init__(
            f"resource limit exceeded: scope={scope!r}, resource={resource!r}, "
            f"limit={limit}, current={current}, requested={requested}"
        )


class UsageReservationError(BudgetError):
    """额度预留不存在、重复结算或处于非法状态。"""


__all__ = [
    "BudgetConfigurationError",
    "BudgetError",
    "ResourceLimitExceededError",
    "UsageReservationError",
]
