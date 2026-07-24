"""提供企业多租户注册、租户级账本隔离与运行归属校验。"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from threading import RLock
from types import MappingProxyType

from matterloop_policies.budget import BudgetLimits
from matterloop_policies.usage import UsageLedger


class TenancyError(Exception):
    """所有多租户领域异常的基类。"""


class TenantNotFoundError(TenancyError):
    """目标租户从未注册。"""

    def __init__(self, tenant_id: str) -> None:
        self.tenant_id = tenant_id
        super().__init__(f"tenant not found: {tenant_id!r}")


class TenantInactiveError(TenancyError):
    """目标租户已被停用，禁止继续操作。"""

    def __init__(self, tenant_id: str) -> None:
        self.tenant_id = tenant_id
        super().__init__(f"tenant is inactive: {tenant_id!r}")


class TenantIsolationError(TenancyError):
    """资源归属租户与请求主体租户不一致。

    异常只包含两个租户标识，不拼接资源内容或请求负载。
    """

    def __init__(self, *, resource_tenant_id: str, principal_tenant_id: str) -> None:
        self.resource_tenant_id = resource_tenant_id
        self.principal_tenant_id = principal_tenant_id
        super().__init__(
            f"tenant isolation violated: resource belongs to {resource_tenant_id!r}, "
            f"principal belongs to {principal_tenant_id!r}"
        )


def _freeze_metadata(metadata: Mapping[str, str]) -> Mapping[str, str]:
    """返回只读的租户元数据映射。"""
    return MappingProxyType(dict(metadata))


def _normalize_tenant_id(tenant_id: str) -> str:
    """去除空白并拒绝空的租户标识。"""
    normalized = tenant_id.strip()
    if not normalized:
        raise ValueError("tenant id must not be empty")
    return normalized


@dataclass(frozen=True, slots=True)
class TenantContext:
    """描述一个企业租户的静态身份信息。

    Args:
        tenant_id: 全局唯一的租户标识。
        display_name: 面向运营界面的租户展示名称。
        tags: 用于路由或分级的只读标签。
        metadata: 由宿主注入的附加元数据，构造后不可修改。
    """

    tenant_id: str
    display_name: str
    tags: tuple[str, ...] = ()
    metadata: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        """规范租户标识并冻结元数据。"""
        object.__setattr__(self, "tenant_id", _normalize_tenant_id(self.tenant_id))
        if not self.display_name.strip():
            raise ValueError("tenant display name must not be empty")
        object.__setattr__(self, "metadata", _freeze_metadata(self.metadata))


class TenantRegistry:
    """线程安全的租户注册表，负责租户生命周期查询。

    停用是不可逆的软删除：租户仍保留在注册表中以便审计，但任何
    ``get`` 都会失败，从而阻止新的租户级操作。
    """

    def __init__(self) -> None:
        self._tenants: dict[str, TenantContext] = {}
        self._inactive: set[str] = set()
        self._lock = RLock()

    def register(self, context: TenantContext) -> None:
        """注册一个新租户，重复注册视为配置错误。

        Args:
            context: 待注册的租户上下文。

        Raises:
            ValueError: 同一 ``tenant_id`` 已经注册过。
        """
        with self._lock:
            if context.tenant_id in self._tenants:
                raise ValueError(f"tenant is already registered: {context.tenant_id!r}")
            self._tenants[context.tenant_id] = context

    def get(self, tenant_id: str) -> TenantContext:
        """返回一个活跃租户的上下文。

        Args:
            tenant_id: 目标租户标识。

        Returns:
            注册时提供的租户上下文。

        Raises:
            TenantNotFoundError: 租户从未注册。
            TenantInactiveError: 租户已被停用。
        """
        normalized = _normalize_tenant_id(tenant_id)
        with self._lock:
            context = self._tenants.get(normalized)
            if context is None:
                raise TenantNotFoundError(normalized)
            if normalized in self._inactive:
                raise TenantInactiveError(normalized)
            return context

    def deactivate(self, tenant_id: str) -> None:
        """停用一个已注册租户，操作幂等。

        Args:
            tenant_id: 目标租户标识。

        Raises:
            TenantNotFoundError: 租户从未注册。
        """
        normalized = _normalize_tenant_id(tenant_id)
        with self._lock:
            if normalized not in self._tenants:
                raise TenantNotFoundError(normalized)
            self._inactive.add(normalized)

    def is_active(self, tenant_id: str) -> bool:
        """判断一个已注册租户是否仍然活跃。

        Args:
            tenant_id: 目标租户标识。

        Returns:
            租户未被停用时返回 ``True``。

        Raises:
            TenantNotFoundError: 租户从未注册。
        """
        normalized = _normalize_tenant_id(tenant_id)
        with self._lock:
            if normalized not in self._tenants:
                raise TenantNotFoundError(normalized)
            return normalized not in self._inactive


TenantLimitsFactory = Callable[[TenantContext], "BudgetLimits | None"]


class TenantScopedLedgers:
    """为每个活跃租户惰性维护一个独立的 :class:`UsageLedger`。

    每个租户拥有各自的账本实例，scope、预留与结算互不可见，
    从而在计量层面实现资源隔离。
    """

    def __init__(
        self,
        registry: TenantRegistry,
        *,
        limits_factory: TenantLimitsFactory | None = None,
    ) -> None:
        self._registry = registry
        self._limits_factory = limits_factory
        self._ledgers: dict[str, UsageLedger] = {}
        self._lock = RLock()

    def ledger_for(self, tenant_id: str) -> UsageLedger:
        """返回目标租户的专属账本，首次访问时惰性创建。

        Args:
            tenant_id: 目标租户标识。

        Returns:
            该租户独立的用量账本。

        Raises:
            TenantNotFoundError: 租户从未注册。
            TenantInactiveError: 租户已被停用。
        """
        context = self._registry.get(tenant_id)
        with self._lock:
            ledger = self._ledgers.get(context.tenant_id)
            if ledger is None:
                limits = None if self._limits_factory is None else self._limits_factory(context)
                ledger = UsageLedger(limits)
                self._ledgers[context.tenant_id] = ledger
            return ledger


class TenantIsolationPolicy:
    """校验运行、检查点等资源的租户归属。

    宿主应在仓储或检查点访问前调用 :meth:`ensure_same_tenant`，
    用资源记录的归属租户与请求主体租户做强一致比较，实现
    Agent 隔离与数据隔离。
    """

    def ensure_same_tenant(self, resource_tenant_id: str, principal_tenant_id: str) -> None:
        """要求资源与请求主体属于同一租户。

        Args:
            resource_tenant_id: 资源记录的归属租户标识。
            principal_tenant_id: 发起请求的主体租户标识。

        Raises:
            TenantIsolationError: 两个租户标识不一致。
        """
        resource = _normalize_tenant_id(resource_tenant_id)
        principal = _normalize_tenant_id(principal_tenant_id)
        if resource != principal:
            raise TenantIsolationError(
                resource_tenant_id=resource,
                principal_tenant_id=principal,
            )


__all__ = [
    "TenancyError",
    "TenantContext",
    "TenantInactiveError",
    "TenantIsolationError",
    "TenantIsolationPolicy",
    "TenantLimitsFactory",
    "TenantNotFoundError",
    "TenantRegistry",
    "TenantScopedLedgers",
]
