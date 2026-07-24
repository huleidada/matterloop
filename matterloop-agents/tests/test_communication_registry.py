"""管理面 Agent 注册表的注册、状态机、权限与版本历史测试。"""

from __future__ import annotations

from dataclasses import replace

import pytest
from matterloop_agents.communication.registry import (
    AgentAlreadyRegisteredError,
    AgentNotRegisteredError,
    AgentRegistration,
    AgentRuntimeStatus,
    AgentSla,
    InvalidStatusTransitionError,
    ManagedAgentRegistry,
)


def _registration(
    agent_id: str = "worker-1",
    *,
    capabilities: tuple[str, ...] = ("python",),
    version: str = "1.0.0",
    status: AgentRuntimeStatus = AgentRuntimeStatus.ACTIVE,
    permissions: tuple[str, ...] = ("tools.read",),
) -> AgentRegistration:
    """构造登记信息的测试辅助函数。"""
    return AgentRegistration(
        agent_id=agent_id,
        capabilities=capabilities,
        version=version,
        status=status,
        permissions=permissions,
    )


class TestRegistrationValidation:
    def test_rejects_empty_agent_id(self) -> None:
        with pytest.raises(ValueError, match="agent_id"):
            _registration(agent_id="  ")

    def test_rejects_invalid_version(self) -> None:
        with pytest.raises(ValueError, match="X.Y.Z"):
            _registration(version="1.0")

    def test_sla_rejects_out_of_range_targets(self) -> None:
        with pytest.raises(ValueError, match="availability_target"):
            AgentSla(availability_target=1.5)
        with pytest.raises(ValueError, match="max_latency_seconds"):
            AgentSla(max_latency_seconds=0)
        with pytest.raises(ValueError, match="max_queue_depth"):
            AgentSla(max_queue_depth=0)

    def test_sla_accepts_valid_targets(self) -> None:
        sla = AgentSla(max_latency_seconds=2.5, availability_target=0.99, max_queue_depth=100)
        assert sla.availability_target == 0.99


class TestRegistrationLifecycle:
    def test_register_and_get(self) -> None:
        registry = ManagedAgentRegistry()
        registration = _registration()
        registry.register(registration)
        assert registry.get("worker-1") is registration

    def test_duplicate_register_raises(self) -> None:
        registry = ManagedAgentRegistry()
        registry.register(_registration())
        with pytest.raises(AgentAlreadyRegisteredError):
            registry.register(_registration())

    def test_replace_updates_registration(self) -> None:
        registry = ManagedAgentRegistry()
        registry.register(_registration(version="1.0.0"))
        registry.replace(_registration(version="1.1.0"))
        assert registry.get("worker-1").version == "1.1.0"

    def test_replace_unregistered_raises(self) -> None:
        registry = ManagedAgentRegistry()
        with pytest.raises(AgentNotRegisteredError):
            registry.replace(_registration())

    def test_deregister_removes_agent(self) -> None:
        registry = ManagedAgentRegistry()
        registry.register(_registration())
        registry.deregister("worker-1")
        with pytest.raises(AgentNotRegisteredError):
            registry.get("worker-1")

    def test_deregister_unknown_raises(self) -> None:
        registry = ManagedAgentRegistry()
        with pytest.raises(AgentNotRegisteredError):
            registry.deregister("ghost")

    def test_list_all_sorted_by_agent_id(self) -> None:
        registry = ManagedAgentRegistry()
        registry.register(_registration("worker-2"))
        registry.register(_registration("worker-1"))
        assert [item.agent_id for item in registry.list_all()] == ["worker-1", "worker-2"]


class TestStatusTransitions:
    @pytest.mark.parametrize(
        ("source", "target"),
        [
            (AgentRuntimeStatus.ACTIVE, AgentRuntimeStatus.DRAINING),
            (AgentRuntimeStatus.ACTIVE, AgentRuntimeStatus.DISABLED),
            (AgentRuntimeStatus.DRAINING, AgentRuntimeStatus.ACTIVE),
            (AgentRuntimeStatus.DRAINING, AgentRuntimeStatus.DISABLED),
            (AgentRuntimeStatus.DISABLED, AgentRuntimeStatus.ACTIVE),
        ],
    )
    def test_legal_transitions(
        self,
        source: AgentRuntimeStatus,
        target: AgentRuntimeStatus,
    ) -> None:
        registry = ManagedAgentRegistry()
        registry.register(_registration(status=source))
        updated = registry.set_status("worker-1", target)
        assert updated.status is target
        assert registry.get("worker-1").status is target

    def test_disabled_cannot_go_directly_to_draining(self) -> None:
        registry = ManagedAgentRegistry()
        registry.register(_registration(status=AgentRuntimeStatus.DISABLED))
        with pytest.raises(InvalidStatusTransitionError):
            registry.set_status("worker-1", AgentRuntimeStatus.DRAINING)

    def test_disabled_reaches_draining_via_active(self) -> None:
        registry = ManagedAgentRegistry()
        registry.register(_registration(status=AgentRuntimeStatus.DISABLED))
        registry.set_status("worker-1", AgentRuntimeStatus.ACTIVE)
        assert registry.set_status("worker-1", AgentRuntimeStatus.DRAINING).status is (
            AgentRuntimeStatus.DRAINING
        )

    def test_same_status_is_idempotent(self) -> None:
        registry = ManagedAgentRegistry()
        registry.register(_registration(status=AgentRuntimeStatus.ACTIVE))
        assert registry.set_status("worker-1", AgentRuntimeStatus.ACTIVE).status is (
            AgentRuntimeStatus.ACTIVE
        )

    def test_set_status_unknown_agent_raises(self) -> None:
        registry = ManagedAgentRegistry()
        with pytest.raises(AgentNotRegisteredError):
            registry.set_status("ghost", AgentRuntimeStatus.ACTIVE)


class TestCapabilityAndPermission:
    def test_find_by_capability_filters_non_active(self) -> None:
        registry = ManagedAgentRegistry()
        registry.register(_registration("active-1", capabilities=("python",)))
        registry.register(
            _registration(
                "draining-1",
                capabilities=("python",),
                status=AgentRuntimeStatus.DRAINING,
            )
        )
        registry.register(
            _registration(
                "disabled-1",
                capabilities=("python",),
                status=AgentRuntimeStatus.DISABLED,
            )
        )
        registry.register(_registration("other-1", capabilities=("chemistry",)))
        found = registry.find_by_capability("python")
        assert [item.agent_id for item in found] == ["active-1"]

    def test_check_permission(self) -> None:
        registry = ManagedAgentRegistry()
        registry.register(_registration(permissions=("tools.read", "memory.write")))
        assert registry.check_permission("worker-1", "memory.write")
        assert not registry.check_permission("worker-1", "tools.execute")

    def test_check_permission_unknown_agent_raises(self) -> None:
        registry = ManagedAgentRegistry()
        with pytest.raises(AgentNotRegisteredError):
            registry.check_permission("ghost", "tools.read")


class TestVersionHistory:
    def test_history_records_register_and_replace_order(self) -> None:
        registry = ManagedAgentRegistry()
        registry.register(_registration(version="1.0.0"))
        registry.replace(_registration(version="1.1.0"))
        registry.replace(_registration(version="2.0.0"))
        assert registry.version_history("worker-1") == ("1.0.0", "1.1.0", "2.0.0")

    def test_history_skips_consecutive_duplicate_versions(self) -> None:
        registry = ManagedAgentRegistry()
        registration = _registration(version="1.0.0")
        registry.register(registration)
        registry.replace(replace(registration, metadata={"note": "hot-fix"}))
        assert registry.version_history("worker-1") == ("1.0.0",)

    def test_history_survives_deregistration(self) -> None:
        registry = ManagedAgentRegistry()
        registry.register(_registration(version="1.0.0"))
        registry.deregister("worker-1")
        assert registry.version_history("worker-1") == ("1.0.0",)

    def test_history_empty_for_unknown_agent(self) -> None:
        registry = ManagedAgentRegistry()
        assert registry.version_history("ghost") == ()
