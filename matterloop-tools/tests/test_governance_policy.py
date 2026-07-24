"""工具风险分级策略测试。"""

import pytest
from matterloop_tools import ToolAccessLevel, ToolPolicy, ToolPolicySet


def test_tool_policy_validates_fields() -> None:
    with pytest.raises(ValueError):
        ToolPolicy(" ", ToolAccessLevel.READ_ONLY, risk_score=10)
    with pytest.raises(ValueError):
        ToolPolicy("shell", ToolAccessLevel.WRITE, risk_score=101)
    with pytest.raises(ValueError):
        ToolPolicy("shell", ToolAccessLevel.WRITE, risk_score=-1)
    with pytest.raises(ValueError):
        ToolPolicy("shell", ToolAccessLevel.WRITE, risk_score=50, max_calls_per_run=0)


def test_tool_policy_is_immutable() -> None:
    policy = ToolPolicy("shell", ToolAccessLevel.WRITE, risk_score=80)

    with pytest.raises(AttributeError):
        policy.risk_score = 0  # type: ignore[misc]


def test_policy_set_registers_and_classifies_known_tool() -> None:
    policy = ToolPolicy("http", ToolAccessLevel.READ_ONLY, risk_score=20, notes="只读接口")
    policies = ToolPolicySet([policy])

    assert policies.names() == ("http",)
    assert policies.get("http") is policy
    assert policies.classify("http") is policy


def test_policy_set_defaults_unknown_tool_to_approval_required() -> None:
    policies = ToolPolicySet()

    fallback = policies.classify("unknown-tool")

    assert policies.default_policy is ToolAccessLevel.APPROVAL_REQUIRED
    assert fallback.tool_name == "unknown-tool"
    assert fallback.access_level is ToolAccessLevel.APPROVAL_REQUIRED
    assert fallback.risk_score == 100
    assert policies.get("unknown-tool") is None


def test_policy_set_honors_configured_default_policy() -> None:
    policies = ToolPolicySet(default_policy=ToolAccessLevel.READ_ONLY)

    assert policies.classify("anything").access_level is ToolAccessLevel.READ_ONLY


def test_policy_set_rejects_duplicate_registration_without_replace() -> None:
    policies = ToolPolicySet([ToolPolicy("shell", ToolAccessLevel.WRITE, risk_score=90)])
    replacement = ToolPolicy("shell", ToolAccessLevel.APPROVAL_REQUIRED, risk_score=95)

    with pytest.raises(ValueError):
        policies.register(replacement)

    policies.register(replacement, replace=True)

    assert policies.classify("shell").access_level is ToolAccessLevel.APPROVAL_REQUIRED
