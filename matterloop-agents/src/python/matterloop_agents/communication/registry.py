"""Agent 管理面注册表：运行状态、SLA、权限与版本历史。"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from enum import Enum
from threading import Lock

from matterloop_agents.communication._immutability import freeze_mapping
from matterloop_agents.communication.contract import (
    AgentContract,
    CommunicationError,
    parse_semantic_version,
)


class RegistryError(CommunicationError):
    """管理面注册表所有异常的基类。"""


class AgentAlreadyRegisteredError(RegistryError):
    """同标识 Agent 已经注册且没有使用替换接口。"""


class AgentNotRegisteredError(RegistryError):
    """注册表中不存在指定的 Agent 标识。"""


class InvalidStatusTransitionError(RegistryError):
    """Agent 运行状态迁移不符合状态机约束。"""


class AgentRuntimeStatus(str, Enum):
    """Agent 在管理面上的运行状态。"""

    ACTIVE = "active"
    DRAINING = "draining"
    DISABLED = "disabled"


_ALLOWED_TRANSITIONS: dict[AgentRuntimeStatus, frozenset[AgentRuntimeStatus]] = {
    AgentRuntimeStatus.ACTIVE: frozenset(
        {AgentRuntimeStatus.DRAINING, AgentRuntimeStatus.DISABLED}
    ),
    AgentRuntimeStatus.DRAINING: frozenset(
        {AgentRuntimeStatus.ACTIVE, AgentRuntimeStatus.DISABLED}
    ),
    AgentRuntimeStatus.DISABLED: frozenset({AgentRuntimeStatus.ACTIVE}),
}


@dataclass(frozen=True, slots=True)
class AgentSla:
    """Agent 的服务等级目标。

    Args:
        max_latency_seconds: 单次调用的最大时延目标秒数；``None`` 表示不约束。
        availability_target: 可用性目标，取值区间 ``[0, 1]``；``None`` 表示不约束。
        max_queue_depth: 收件队列深度上限；``None`` 表示不约束。
    """

    max_latency_seconds: float | None = None
    availability_target: float | None = None
    max_queue_depth: int | None = None

    def __post_init__(self) -> None:
        """校验各项目标的取值范围。"""
        if self.max_latency_seconds is not None and self.max_latency_seconds <= 0:
            raise ValueError("max_latency_seconds must be positive when provided")
        if self.availability_target is not None and not 0 <= self.availability_target <= 1:
            raise ValueError("availability_target must be within [0, 1] when provided")
        if self.max_queue_depth is not None and self.max_queue_depth < 1:
            raise ValueError("max_queue_depth must be at least 1 when provided")


@dataclass(frozen=True, slots=True)
class AgentRegistration:
    """Agent 在管理面注册表中的不可变登记信息。

    Args:
        agent_id: Agent 的稳定标识。
        capabilities: Agent 声明的能力名集合。
        version: ``X.Y.Z`` 语义化版本字符串，构造时校验格式。
        contract: 可选的 Agent 契约。
        status: Agent 当前运行状态。
        permissions: Agent 被授予的权限名集合。
        sla: 可选的服务等级目标。
        metadata: 不参与路由与查询的只读扩展信息。
    """

    agent_id: str
    capabilities: tuple[str, ...] = ()
    version: str = "0.1.0"
    contract: AgentContract | None = None
    status: AgentRuntimeStatus = AgentRuntimeStatus.ACTIVE
    permissions: tuple[str, ...] = ()
    sla: AgentSla | None = None
    metadata: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        """校验标识与版本并冻结扩展信息。"""
        if not self.agent_id.strip():
            raise ValueError("agent_id must not be empty")
        parse_semantic_version(self.version)
        object.__setattr__(self, "metadata", freeze_mapping(self.metadata))


class ManagedAgentRegistry:
    """线程安全的 Agent 管理面注册表。

    维护注册信息、运行状态状态机、权限与版本历史。状态机约束：``DISABLED``
    不能直接迁移到 ``DRAINING``，必须先回到 ``ACTIVE``。
    """

    def __init__(self) -> None:
        self._registrations: dict[str, AgentRegistration] = {}
        self._version_history: dict[str, list[str]] = {}
        self._lock = Lock()

    def register(self, registration: AgentRegistration) -> None:
        """注册一个新的 Agent。

        Args:
            registration: 待注册的登记信息。

        Raises:
            AgentAlreadyRegisteredError: 同标识 Agent 已经注册；替换请使用
                :meth:`replace`。
        """
        with self._lock:
            if registration.agent_id in self._registrations:
                raise AgentAlreadyRegisteredError(
                    f"agent is already registered: {registration.agent_id}"
                )
            self._registrations[registration.agent_id] = registration
            self._record_version(registration)

    def replace(self, registration: AgentRegistration) -> None:
        """原子替换已注册 Agent 的登记信息。

        Args:
            registration: 新的登记信息，标识必须已经注册。

        Raises:
            AgentNotRegisteredError: 指定标识没有注册。
        """
        with self._lock:
            if registration.agent_id not in self._registrations:
                raise AgentNotRegisteredError(f"agent is not registered: {registration.agent_id}")
            self._registrations[registration.agent_id] = registration
            self._record_version(registration)

    def deregister(self, agent_id: str) -> None:
        """注销一个 Agent；版本历史继续保留以供审计。

        Args:
            agent_id: 待注销的 Agent 标识。

        Raises:
            AgentNotRegisteredError: 指定标识没有注册。
        """
        with self._lock:
            if agent_id not in self._registrations:
                raise AgentNotRegisteredError(f"agent is not registered: {agent_id}")
            del self._registrations[agent_id]

    def get(self, agent_id: str) -> AgentRegistration:
        """查询指定 Agent 的当前登记信息。

        Args:
            agent_id: Agent 标识。

        Returns:
            当前登记信息。

        Raises:
            AgentNotRegisteredError: 指定标识没有注册。
        """
        with self._lock:
            registration = self._registrations.get(agent_id)
        if registration is None:
            raise AgentNotRegisteredError(f"agent is not registered: {agent_id}")
        return registration

    def find_by_capability(self, capability: str) -> tuple[AgentRegistration, ...]:
        """查询具备指定能力且状态为 ``ACTIVE`` 的全部 Agent。

        Args:
            capability: 需要匹配的能力名。

        Returns:
            按 ``agent_id`` 稳定排序的匹配登记信息。
        """
        with self._lock:
            return tuple(
                registration
                for _, registration in sorted(self._registrations.items())
                if registration.status is AgentRuntimeStatus.ACTIVE
                and capability in registration.capabilities
            )

    def list_all(self) -> tuple[AgentRegistration, ...]:
        """返回按 ``agent_id`` 稳定排序的全部登记信息快照。"""
        with self._lock:
            return tuple(registration for _, registration in sorted(self._registrations.items()))

    def set_status(self, agent_id: str, status: AgentRuntimeStatus) -> AgentRegistration:
        """迁移 Agent 的运行状态。

        合法迁移：``ACTIVE -> DRAINING/DISABLED``、``DRAINING -> ACTIVE/DISABLED``、
        ``DISABLED -> ACTIVE``。迁移到当前状态是幂等空操作。

        Args:
            agent_id: Agent 标识。
            status: 目标运行状态。

        Returns:
            迁移后的登记信息。

        Raises:
            AgentNotRegisteredError: 指定标识没有注册。
            InvalidStatusTransitionError: 状态迁移不合法，例如 ``DISABLED``
                直接迁移到 ``DRAINING``。
        """
        with self._lock:
            registration = self._registrations.get(agent_id)
            if registration is None:
                raise AgentNotRegisteredError(f"agent is not registered: {agent_id}")
            if registration.status is status:
                return registration
            if status not in _ALLOWED_TRANSITIONS[registration.status]:
                raise InvalidStatusTransitionError(
                    f"illegal status transition for {agent_id}:"
                    f" {registration.status.value} -> {status.value}"
                )
            updated = replace(registration, status=status)
            self._registrations[agent_id] = updated
            return updated

    def check_permission(self, agent_id: str, permission: str) -> bool:
        """检查 Agent 是否被授予指定权限。

        Args:
            agent_id: Agent 标识。
            permission: 待检查的权限名。

        Returns:
            持有该权限时为 ``True``。

        Raises:
            AgentNotRegisteredError: 指定标识没有注册。
        """
        with self._lock:
            registration = self._registrations.get(agent_id)
        if registration is None:
            raise AgentNotRegisteredError(f"agent is not registered: {agent_id}")
        return permission in registration.permissions

    def version_history(self, agent_id: str) -> tuple[str, ...]:
        """返回 Agent 按登记顺序排列的版本历史。

        历史在注销后仍然保留；连续重复登记同一版本只记录一次。

        Args:
            agent_id: Agent 标识。

        Returns:
            版本字符串元组；从未登记过时为空元组。
        """
        with self._lock:
            return tuple(self._version_history.get(agent_id, ()))

    def _record_version(self, registration: AgentRegistration) -> None:
        """在锁内追加版本历史，跳过连续重复版本。"""
        history = self._version_history.setdefault(registration.agent_id, [])
        if not history or history[-1] != registration.version:
            history.append(registration.version)


__all__ = [
    "AgentAlreadyRegisteredError",
    "AgentNotRegisteredError",
    "AgentRegistration",
    "AgentRuntimeStatus",
    "AgentSla",
    "InvalidStatusTransitionError",
    "ManagedAgentRegistry",
    "RegistryError",
]
