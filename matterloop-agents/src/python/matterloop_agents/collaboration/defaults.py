"""多智能体协作层可直接使用的无外部依赖默认组件。"""

from __future__ import annotations

from matterloop_core import ApprovalDecision

from matterloop_agents.collaboration.models import (
    AgentTaskContext,
    TaskResult,
    TaskSpec,
    TaskVerification,
    TeamPlanningContext,
    TeamRequest,
    TeamReview,
    TeamReviewAction,
    TeamReviewContext,
)


class StaticTeamPlanner:
    """返回构造时注入的固定任务图。

    该规划器适合声明式工作流、测试和由业务代码预先生成任务图的场景；它不会读取
    环境变量，也不会隐式构造模型客户端。

    Args:
        tasks: 每次规划都要返回的不可变任务定义。
    """

    def __init__(self, tasks: tuple[TaskSpec, ...]) -> None:
        if not tasks:
            raise ValueError("tasks must not be empty")
        self._tasks = tuple(tasks)

    async def plan(self, context: TeamPlanningContext) -> tuple[TaskSpec, ...]:
        """返回固定任务图。

        Args:
            context: 本次循环、团队目标和能力快照。

        Returns:
            构造时注入的任务元组。
        """
        del context
        return self._tasks


class AcceptAllTeamReviewer:
    """接受所有已经通过任务级验证的团队草稿。

    该默认实现用于声明式流程和测试。需要总体目标语义验收时，应注入
    领域审查器或 ``ModelTeamReviewer``。
    """

    async def review(self, context: TeamReviewContext) -> TeamReview:
        """返回固定接受决策。

        Args:
            context: 已通过任务级验证的团队草稿。

        Returns:
            不含额外问题的接受审查。
        """
        del context
        return TeamReview(action=TeamReviewAction.ACCEPT, score=100.0)


class ResultSuccessVerifier:
    """只根据端点的结构化成功标志进行基础验证。

    该实现不声称能够语义验证验收条件。生产装配应注入领域验证器或模型验证器。
    """

    async def verify(
        self,
        context: AgentTaskContext,
        result: TaskResult,
    ) -> TaskVerification:
        """把任务结果的成功标志转换为标准验证结论。

        Args:
            context: 当前任务执行上下文。
            result: Agent 返回的结构化结果。

        Returns:
            与 ``result.success`` 一致的验证结果。
        """
        del context
        return TaskVerification(
            passed=result.success,
            feedback=result.error,
            score=100.0 if result.success else 0.0,
        )


class AlwaysApproveTeamGate:
    """批准所有显式要求审批的任务。"""

    async def decide(self, context: AgentTaskContext) -> ApprovalDecision:
        """返回批准决策。

        Args:
            context: 等待审批的任务上下文。

        Returns:
            固定的批准决策。
        """
        del context
        return ApprovalDecision.APPROVED


class ConcatenateResultAggregator:
    """按任务结果顺序连接非空文本输出。"""

    def __init__(self, *, separator: str = "\n\n") -> None:
        self._separator = separator

    async def aggregate(
        self,
        request: TeamRequest,
        results: tuple[TaskResult, ...],
    ) -> str:
        """连接所有成功任务的非空输出。

        Args:
            request: 原始团队请求。
            results: 已通过验证的任务结果。

        Returns:
            使用注入分隔符连接的团队输出。
        """
        del request
        return self._separator.join(result.output for result in results if result.output)


__all__ = [
    "AcceptAllTeamReviewer",
    "AlwaysApproveTeamGate",
    "ConcatenateResultAggregator",
    "ResultSuccessVerifier",
    "StaticTeamPlanner",
]
