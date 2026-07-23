"""基于规则的访问控制测试。"""

import pytest
from matterloop_tools import AccessRule, Principal, RuleBasedAccessController


def _principal(
    agent_id: str = "agent-1",
    user_id: str | None = "user-1",
    tenant_id: str | None = "tenant-a",
    roles: tuple[str, ...] = (),
) -> Principal:
    return Principal(agent_id, user_id=user_id, tenant_id=tenant_id, roles=roles)


def test_principal_validates_identity() -> None:
    with pytest.raises(ValueError):
        Principal(" ")
    with pytest.raises(ValueError):
        Principal("agent-1", roles=("",))


def test_access_rule_requires_tool_patterns() -> None:
    with pytest.raises(ValueError):
        AccessRule(allowed_tools=())


async def test_controller_denies_by_default() -> None:
    controller = RuleBasedAccessController()

    decision = await controller.authorize(_principal(), "shell", {})

    assert not decision.allowed
    assert "no access rule" in decision.reason


async def test_controller_matches_fnmatch_patterns() -> None:
    controller = RuleBasedAccessController(
        [AccessRule(allowed_tools=("mcp__lab__*", "filesystem"))]
    )

    allowed = await controller.authorize(_principal(), "mcp__lab__query", {})
    exact = await controller.authorize(_principal(), "filesystem", {})
    denied = await controller.authorize(_principal(), "shell", {})

    assert allowed.allowed
    assert exact.allowed
    assert not denied.allowed


async def test_controller_isolates_by_tenant() -> None:
    controller = RuleBasedAccessController([AccessRule(allowed_tools=("*",), tenant_id="tenant-a")])

    same_tenant = await controller.authorize(_principal(tenant_id="tenant-a"), "shell", {})
    other_tenant = await controller.authorize(_principal(tenant_id="tenant-b"), "shell", {})
    no_tenant = await controller.authorize(_principal(tenant_id=None), "shell", {})

    assert same_tenant.allowed
    assert not other_tenant.allowed
    assert not no_tenant.allowed


async def test_controller_isolates_by_user() -> None:
    controller = RuleBasedAccessController([AccessRule(allowed_tools=("*",), user_id="user-1")])

    same_user = await controller.authorize(_principal(user_id="user-1"), "shell", {})
    other_user = await controller.authorize(_principal(user_id="user-2"), "shell", {})

    assert same_user.allowed
    assert not other_user.allowed


async def test_controller_isolates_by_agent() -> None:
    controller = RuleBasedAccessController([AccessRule(allowed_tools=("*",), agent_id="agent-1")])

    same_agent = await controller.authorize(_principal(agent_id="agent-1"), "shell", {})
    other_agent = await controller.authorize(_principal(agent_id="agent-2"), "shell", {})

    assert same_agent.allowed
    assert not other_agent.allowed


async def test_controller_matches_role_condition() -> None:
    controller = RuleBasedAccessController([AccessRule(allowed_tools=("*",), role="operator")])

    with_role = await controller.authorize(_principal(roles=("operator", "viewer")), "shell", {})
    without_role = await controller.authorize(_principal(roles=("viewer",)), "shell", {})

    assert with_role.allowed
    assert not without_role.allowed


async def test_rule_requires_all_conditions_to_match() -> None:
    controller = RuleBasedAccessController(
        [
            AccessRule(
                allowed_tools=("shell",),
                role="operator",
                agent_id="agent-1",
                tenant_id="tenant-a",
            )
        ]
    )

    full_match = await controller.authorize(
        _principal(agent_id="agent-1", tenant_id="tenant-a", roles=("operator",)),
        "shell",
        {},
    )
    wrong_agent = await controller.authorize(
        _principal(agent_id="agent-2", tenant_id="tenant-a", roles=("operator",)),
        "shell",
        {},
    )

    assert full_match.allowed
    assert not wrong_agent.allowed
