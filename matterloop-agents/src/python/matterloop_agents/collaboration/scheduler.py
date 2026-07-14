"""确定性的最少负载 Agent 选择策略。"""

from __future__ import annotations

from collections.abc import Mapping

from matterloop_agents.collaboration.errors import (
    AgentCapacityError,
    NoCapableAgentError,
)
from matterloop_agents.collaboration.models import AgentSpec, TaskSpec


class LeastBusyScheduler:
    """先匹配能力和容量，再按活跃数及 ``agent_id`` 稳定选择。"""

    async def select(
        self,
        task: TaskSpec,
        candidates: tuple[AgentSpec, ...],
        active_counts: Mapping[str, int],
    ) -> str:
        """返回满足能力要求的最少负载 Agent。

        Args:
            task: 带单个所需能力标签的任务。
            candidates: 当前目录的 Agent 发现快照。
            active_counts: 与候选快照对应的活跃租约数。

        Returns:
            负载最低；负载相同时 ``agent_id`` 字典序最小的 Agent 标识。

        Raises:
            ValueError: 活跃计数为负数。
            NoCapableAgentError: 没有 Agent 具备任务要求的能力。
            AgentCapacityError: 所有能力匹配的 Agent 都已达到并发上限。
        """
        capable: list[tuple[AgentSpec, int]] = []
        for candidate in candidates:
            active_count = active_counts.get(candidate.agent_id, 0)
            if active_count < 0:
                raise ValueError(f"active count must not be negative: {candidate.agent_id}")
            if task.capability in candidate.capabilities:
                capable.append((candidate, active_count))
        if not capable:
            raise NoCapableAgentError(
                f"no agent has capability {task.capability!r} for task: {task.task_id}"
            )

        available = (
            (active_count, candidate.agent_id)
            for candidate, active_count in capable
            if active_count < candidate.max_concurrency
        )
        try:
            return min(available)[1]
        except ValueError as exc:
            raise AgentCapacityError(
                f"all capable agents are at capacity for task: {task.task_id}"
            ) from exc


__all__ = ["LeastBusyScheduler"]
