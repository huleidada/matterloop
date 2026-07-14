"""支持并发容量与原子热替换的 Agent 目录。"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from threading import RLock
from types import MappingProxyType

from matterloop_agents.collaboration.errors import (
    AgentAlreadyRegisteredError,
    AgentCapacityError,
    AgentNotFoundError,
    NoCapableAgentError,
)
from matterloop_agents.collaboration.models import AgentSpec, TaskSpec
from matterloop_agents.collaboration.protocols import AgentEndpoint, AgentSelectionPolicy


@dataclass(frozen=True, slots=True)
class AgentLease:
    """持有一次分派期间固定的 Agent 规范和端点。

    即使目录中的同名 Agent 被替换，已经取得的租约仍继续使用取得时的端点。

    Args:
        spec: 取得租约时对应的 Agent 发现信息。
        endpoint: 本次任务必须使用的固定端点实例。
    """

    spec: AgentSpec
    endpoint: AgentEndpoint


@dataclass(slots=True)
class _AgentSlot:
    spec: AgentSpec
    endpoint: AgentEndpoint
    active_count: int = 0
    generation: int = 0
    registered: bool = True


class AgentDirectory:
    """线程安全地管理 Agent 注册、选择和并发租约。

    注册表变更和容量计数都在同一把可重入锁下完成。热替换只改变后续租约使用的端点；
    活跃计数保留在稳定槽位中，因此替换不能绕过 ``AgentSpec.max_concurrency``。
    本组件不拥有端点生命周期，也不会启动或关闭端点。
    """

    def __init__(self) -> None:
        self._slots: dict[str, _AgentSlot] = {}
        self._lock = RLock()

    def register(self, endpoint: AgentEndpoint, *, replace: bool = False) -> None:
        """注册端点，或在显式允许时原子替换同名端点。

        Args:
            endpoint: 带稳定 ``AgentSpec`` 的端点。
            replace: 是否允许原子替换已经存在的同名端点。

        Raises:
            AgentAlreadyRegisteredError: 同名 Agent 已存在且未允许替换。
        """
        spec = endpoint.spec
        with self._lock:
            current = self._slots.get(spec.agent_id)
            if current is None:
                self._slots[spec.agent_id] = _AgentSlot(spec, endpoint)
                return
            if not current.registered:
                self._replace_slot(current, spec, endpoint)
                current.registered = True
                return
            if not replace:
                raise AgentAlreadyRegisteredError(f"agent is already registered: {spec.agent_id}")
            self._replace_slot(current, spec, endpoint)

    def replace(self, agent_id: str, endpoint: AgentEndpoint) -> None:
        """原子替换指定 Agent 的发现信息和后续调用端点。

        Args:
            agent_id: 当前目录中的稳定 Agent 标识。
            endpoint: 规范标识必须与 ``agent_id`` 相同的新端点。

        Raises:
            AgentNotFoundError: 指定 Agent 不存在。
            ValueError: 新端点声明了不同的 Agent 标识。
        """
        spec = endpoint.spec
        if spec.agent_id != agent_id:
            raise ValueError("replacement endpoint agent_id must match directory agent_id")
        with self._lock:
            current = self._slots.get(agent_id)
            if current is None or not current.registered:
                raise AgentNotFoundError(f"agent is not registered: {agent_id}")
            self._replace_slot(current, spec, endpoint)

    def unregister(self, agent_id: str) -> None:
        """从后续发现和分派中移除 Agent。

        已经取得的租约继续持有原端点并在退出时安全归还其容量计数。

        Args:
            agent_id: 需要移除的 Agent 标识。

        Raises:
            AgentNotFoundError: 指定 Agent 不存在。
        """
        with self._lock:
            slot = self._slots.get(agent_id)
            if slot is None or not slot.registered:
                raise AgentNotFoundError(f"agent is not registered: {agent_id}")
            slot.registered = False
            slot.generation += 1
            if slot.active_count == 0:
                self._slots.pop(agent_id, None)

    def candidates(self) -> tuple[AgentSpec, ...]:
        """返回按 ``agent_id`` 稳定排序的 Agent 发现快照。"""
        with self._lock:
            return tuple(slot.spec for _, slot in sorted(self._slots.items()) if slot.registered)

    @asynccontextmanager
    async def acquire(
        self,
        task: TaskSpec,
        policy: AgentSelectionPolicy,
    ) -> AsyncIterator[AgentLease]:
        """原子选择并租用一个未超过并发上限的 Agent。

        选择策略在锁外异步执行；策略返回后，目录会校验注册代次和容量。如果选择期间发生
        注册变更或其他调用抢占了最后容量，目录会基于新快照重新选择。

        Args:
            task: 等待分派的任务。
            policy: 能力和负载选择策略。

        Yields:
            在整个上下文期间固定端点实例的 Agent 租约。

        Raises:
            NoCapableAgentError: 目录为空或没有具备任务能力的 Agent。
            AgentCapacityError: 所有匹配 Agent 已满，或策略返回了已满 Agent。
            AgentNotFoundError: 策略返回了候选快照以外的 Agent。
        """
        selected_slot: _AgentSlot
        lease: AgentLease
        while True:
            with self._lock:
                registered = {
                    agent_id: slot for agent_id, slot in self._slots.items() if slot.registered
                }
                if not registered:
                    raise NoCapableAgentError(f"no agents are registered for task: {task.task_id}")
                snapshots = {
                    agent_id: (slot, slot.generation, slot.spec, slot.active_count)
                    for agent_id, slot in registered.items()
                }
                candidates = tuple(snapshot[2] for _, snapshot in sorted(snapshots.items()))
                active_counts = MappingProxyType(
                    {agent_id: snapshot[3] for agent_id, snapshot in sorted(snapshots.items())}
                )

            selected_id = await policy.select(task, candidates, active_counts)
            selected_snapshot = snapshots.get(selected_id)
            if selected_snapshot is None:
                raise AgentNotFoundError(
                    f"selection policy returned an unknown agent: {selected_id}"
                )
            snapshot_slot, generation, snapshot_spec, snapshot_active = selected_snapshot
            if snapshot_active >= snapshot_spec.max_concurrency:
                raise AgentCapacityError(
                    f"selection policy returned an agent at capacity: {selected_id}"
                )

            with self._lock:
                current = self._slots.get(selected_id)
                if current is not snapshot_slot or current.generation != generation:
                    continue
                if current.active_count >= current.spec.max_concurrency:
                    continue
                current.active_count += 1
                selected_slot = current
                lease = AgentLease(current.spec, current.endpoint)
                break

        try:
            yield lease
        finally:
            with self._lock:
                if selected_slot.active_count <= 0:
                    raise RuntimeError("agent lease capacity counter is inconsistent")
                selected_slot.active_count -= 1
                if not selected_slot.registered and selected_slot.active_count == 0:
                    agent_id = selected_slot.spec.agent_id
                    if self._slots.get(agent_id) is selected_slot:
                        self._slots.pop(agent_id, None)

    @staticmethod
    def _replace_slot(
        slot: _AgentSlot,
        spec: AgentSpec,
        endpoint: AgentEndpoint,
    ) -> None:
        """在锁内更新稳定槽位，同时保留活跃租约计数。"""
        slot.spec = spec
        slot.endpoint = endpoint
        slot.generation += 1


__all__ = ["AgentDirectory", "AgentLease"]
