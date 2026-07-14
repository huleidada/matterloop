"""多 Agent 协作编排的稳定扩展协议。"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Protocol, runtime_checkable

from matterloop_core import ApprovalDecision

from matterloop_agents.collaboration.events import TeamEvent
from matterloop_agents.collaboration.models import (
    AgentSpec,
    AgentTaskContext,
    TaskResult,
    TaskSpec,
    TaskVerification,
    TeamPlanningContext,
    TeamRequest,
    TeamReview,
    TeamReviewContext,
    TeamSnapshot,
)


@runtime_checkable
class TeamPlanner(Protocol):
    """把团队请求拆成具有明确依赖关系的任务。"""

    async def plan(self, context: TeamPlanningContext) -> tuple[TaskSpec, ...]:
        """生成团队需要执行的任务。

        Args:
            context: 团队目标、循环历史和当前能力目录。

        Returns:
            可按依赖关系调度的任务元组。
        """
        ...


@runtime_checkable
class TeamReviewer(Protocol):
    """在全部任务通过后独立验收团队总体目标。"""

    async def review(self, context: TeamReviewContext) -> TeamReview:
        """返回接受、重规划、请求人工或停止决策。

        Args:
            context: 当前循环的已验证结果、草稿和历史反馈。

        Returns:
            团队级结构化审查结论。
        """
        ...


@runtime_checkable
class AgentEndpoint(Protocol):
    """可由目录发现并执行单个协作任务的 Agent 端点。"""

    @property
    def spec(self) -> AgentSpec:
        """返回端点不可变的发现信息和并发上限。"""
        ...

    async def execute(self, context: AgentTaskContext) -> TaskResult:
        """执行已经分配给当前 Agent 的任务。

        Args:
            context: 团队快照、任务和尝试次数等执行上下文。

        Returns:
            Agent 产生的结构化任务结果。
        """
        ...


@runtime_checkable
class TaskVerifier(Protocol):
    """独立验证一个 Agent 的任务结果。"""

    async def verify(
        self,
        context: AgentTaskContext,
        result: TaskResult,
    ) -> TaskVerification:
        """按照任务验收条件返回结构化验证结论。

        Args:
            context: 当前任务执行上下文。
            result: 等待验证的 Agent 结果。

        Returns:
            包含通过状态、反馈和证据的验证结果。
        """
        ...


@runtime_checkable
class TeamApprovalGate(Protocol):
    """在任务分派前执行团队级审批。"""

    async def decide(self, context: AgentTaskContext) -> ApprovalDecision:
        """返回当前任务的审批决策。

        Args:
            context: 等待审批的任务执行上下文。

        Returns:
            核心统一定义的审批决策。
        """
        ...


@runtime_checkable
class AgentSelectionPolicy(Protocol):
    """根据任务要求和目录负载选择一个 Agent。"""

    async def select(
        self,
        task: TaskSpec,
        candidates: tuple[AgentSpec, ...],
        active_counts: Mapping[str, int],
    ) -> str:
        """返回要租用的 Agent 标识。

        Args:
            task: 等待分派的任务。
            candidates: 当前目录中稳定排序的 Agent 发现信息。
            active_counts: 与候选快照对应的活跃租约计数。

        Returns:
            候选集合中的稳定 Agent 标识。
        """
        ...


@runtime_checkable
class TeamRepository(Protocol):
    """持久化团队快照，并提供 CAS 与跨控制器执行租约。"""

    async def create(self, snapshot: TeamSnapshot) -> None:
        """创建一条尚不存在的团队快照。

        Args:
            snapshot: 初始团队快照。
        """
        ...

    async def load(self, run_id: str) -> TeamSnapshot | None:
        """按运行标识读取团队快照。

        Args:
            run_id: 团队运行标识。

        Returns:
            已保存快照；不存在时返回 ``None``。
        """
        ...

    async def save(
        self,
        snapshot: TeamSnapshot,
        expected_version: int,
    ) -> TeamSnapshot:
        """只在版本匹配时保存团队快照。

        Args:
            snapshot: 新的完整快照。
            expected_version: 调用方最后观察到的版本。

        Returns:
            仓储实际保存且版本已经递增的快照。
        """
        ...

    async def list(self) -> tuple[TeamSnapshot, ...]:
        """返回仓储中稳定排序的全部团队快照。"""
        ...

    async def acquire_lease(self, run_id: str, owner_id: str) -> bool:
        """原子取得运行级独占执行租约。

        Args:
            run_id: 团队运行标识。
            owner_id: 当前控制器实例的稳定标识。

        Returns:
            是否成功取得租约。持久化实现必须自行处理进程崩溃后的过期租约。
        """
        ...

    async def release_lease(self, run_id: str, owner_id: str) -> None:
        """仅在所有者匹配时释放运行级执行租约。

        Args:
            run_id: 团队运行标识。
            owner_id: 当前控制器实例的稳定标识。
        """
        ...


@runtime_checkable
class TeamEventPublisher(Protocol):
    """发布团队协作生命周期事件。"""

    async def publish(self, event: TeamEvent) -> None:
        """发布一个不可变团队事件。

        Args:
            event: 等待发布的团队生命周期事件。
        """
        ...


@runtime_checkable
class ResultAggregator(Protocol):
    """把多个任务结果聚合为团队最终输出。"""

    async def aggregate(
        self,
        request: TeamRequest,
        results: tuple[TaskResult, ...],
    ) -> str:
        """生成团队面向调用方的最终文本结果。

        Args:
            request: 原始团队请求。
            results: 已验证通过的任务结果。

        Returns:
            聚合后的最终输出。
        """
        ...


__all__ = [
    "AgentEndpoint",
    "AgentSelectionPolicy",
    "ResultAggregator",
    "TaskVerifier",
    "TeamApprovalGate",
    "TeamEventPublisher",
    "TeamPlanner",
    "TeamReviewer",
    "TeamRepository",
]
