"""多租户注册、租户级账本隔离与归属校验测试。"""

from __future__ import annotations

import pytest
from matterloop_policies import (
    BudgetLimits,
    ResourceLimitExceededError,
    TenantContext,
    TenantInactiveError,
    TenantIsolationError,
    TenantIsolationPolicy,
    TenantNotFoundError,
    TenantRegistry,
    TenantScopedLedgers,
    UsageAmount,
)


def _registry_with(*tenant_ids: str) -> TenantRegistry:
    """构造包含指定租户的注册表。"""
    registry = TenantRegistry()
    for tenant_id in tenant_ids:
        registry.register(TenantContext(tenant_id, display_name=f"租户 {tenant_id}"))
    return registry


def test_registry_registers_queries_and_deactivates_tenants() -> None:
    """注册后可查询，停用后任何租户级操作都必须失败。"""
    registry = TenantRegistry()
    context = TenantContext(
        " acme ",
        display_name="Acme 材料",
        tags=("enterprise",),
        metadata={"region": "cn-east"},
    )
    registry.register(context)

    assert registry.get("acme") is context
    assert registry.get("acme").tenant_id == "acme"
    assert registry.is_active("acme")
    with pytest.raises(ValueError):
        registry.register(TenantContext("acme", display_name="重复租户"))
    with pytest.raises(TypeError):
        context.metadata["region"] = "cn-north"  # type: ignore[index]

    registry.deactivate("acme")
    assert not registry.is_active("acme")
    with pytest.raises(TenantInactiveError):
        registry.get("acme")


def test_registry_rejects_unknown_tenants() -> None:
    """未注册租户的查询与停用必须抛出 TenantNotFoundError。"""
    registry = _registry_with("acme")

    with pytest.raises(TenantNotFoundError):
        registry.get("ghost")
    with pytest.raises(TenantNotFoundError):
        registry.deactivate("ghost")
    with pytest.raises(TenantNotFoundError):
        registry.is_active("ghost")


def test_scoped_ledgers_isolate_usage_between_tenants() -> None:
    """两个租户各自 reserve 后，账本用量与限额互不影响。"""
    registry = _registry_with("acme", "globex")
    ledgers = TenantScopedLedgers(
        registry,
        limits_factory=lambda context: BudgetLimits(max_tool_calls=1),
    )
    acme = ledgers.ledger_for("acme")
    globex = ledgers.ledger_for("globex")

    assert acme is not globex
    assert ledgers.ledger_for("acme") is acme
    acme.reserve("run", UsageAmount(tool_calls=1))
    globex_reservation = globex.reserve("run", UsageAmount(tool_calls=1))

    assert acme.snapshot("run").reserved.tool_calls == 1
    assert globex.snapshot("run").reserved.tool_calls == 1
    globex.commit(globex_reservation)
    assert globex.snapshot("run").tool_calls == 1
    assert acme.snapshot("run").tool_calls == 0
    # acme 的预留仍占用其独立限额，但不会消耗 globex 的额度。
    with pytest.raises(ResourceLimitExceededError):
        acme.reserve("run", UsageAmount(tool_calls=1))


def test_scoped_ledgers_reject_unknown_and_inactive_tenants() -> None:
    """未注册与停用租户都不能获得账本。"""
    registry = _registry_with("acme")
    ledgers = TenantScopedLedgers(registry)

    with pytest.raises(TenantNotFoundError):
        ledgers.ledger_for("ghost")
    registry.deactivate("acme")
    with pytest.raises(TenantInactiveError):
        ledgers.ledger_for("acme")


def test_isolation_policy_rejects_cross_tenant_resources() -> None:
    """资源归属与请求主体租户不一致时必须抛出隔离异常。"""
    policy = TenantIsolationPolicy()

    policy.ensure_same_tenant("acme", " acme ")
    with pytest.raises(TenantIsolationError) as captured:
        policy.ensure_same_tenant("acme", "globex")

    assert captured.value.resource_tenant_id == "acme"
    assert captured.value.principal_tenant_id == "globex"
