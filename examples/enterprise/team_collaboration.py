"""演示 TeamLoop 的 DAG 并行、人工修订和多层计算额度。"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field

from matterloop_agents.collaboration import (
    AgentDirectory,
    AgentSpec,
    AgentTaskContext,
    AlwaysApproveTeamGate,
    AsyncTeamRuntime,
    ConcatenateResultAggregator,
    InMemoryTeamRepository,
    LeastBusyScheduler,
    LocalTeamEventPublisher,
    ResultSuccessVerifier,
    TaskResult,
    TaskSpec,
    TeamEvent,
    TeamLimits,
    TeamOrchestrator,
    TeamOrchestratorComponents,
    TeamPlanningContext,
    TeamRequest,
    TeamReview,
    TeamReviewAction,
    TeamReviewContext,
    TeamStatus,
    TeamStopReason,
)
from matterloop_core import (
    HumanAction,
    HumanInteractionKind,
    HumanInteractionRequest,
    HumanResponse,
)
from matterloop_policies import BudgetedAgentEndpoint, BudgetLimits, UsageLedger

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class ConcurrencyProbe:
    """记录示例执行期间实际观察到的并发任务数量。"""

    active: int = 0
    maximum: int = 0
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    async def run_once(self) -> None:
        """进入一次短暂异步临界区并更新最大并发值。"""
        async with self.lock:
            self.active += 1
            self.maximum = max(self.maximum, self.active)
        try:
            await asyncio.sleep(0.01)
        finally:
            async with self.lock:
                self.active -= 1


@dataclass(slots=True)
class DeterministicAgentEndpoint:
    """根据任务能力返回确定性结果的无外部依赖 Agent。"""

    spec: AgentSpec
    probe: ConcurrencyProbe
    calls: list[AgentTaskContext] = field(default_factory=list)

    async def execute(self, context: AgentTaskContext) -> TaskResult:
        """执行任务并保留依赖结果数量。

        Args:
            context: 控制器生成的隔离任务上下文。

        Returns:
            与任务和 Agent 标识一致的成功结果。
        """
        self.calls.append(context)
        await self.probe.run_once()
        dependency_summary = (
            ",".join(result.task_id for result in context.dependency_results) or "none"
        )
        feedback = tuple(
            record.response.content for record in context.human_feedback if record.response.content
        )
        return TaskResult(
            task_id=context.task.task_id,
            agent_id=context.agent_id,
            success=True,
            output=(
                f"{context.task.capability}:dependencies={dependency_summary};"
                f"feedback={'|'.join(feedback) or 'none'}"
            ),
            attempt=context.attempt,
        )


class ParallelTeamPlanner:
    """生成两个并行任务和一个 fan-in 汇总任务。"""

    def __init__(self) -> None:
        self.contexts: list[TeamPlanningContext] = []

    async def plan(self, context: TeamPlanningContext) -> tuple[TaskSpec, ...]:
        """按当前 cycle 生成稳定且无未知能力的 DAG。

        Args:
            context: 包含能力快照、审查历史和人工反馈的规划上下文。

        Returns:
            facts 与 risks 并行、summary 等待两者的任务定义。
        """
        self.contexts.append(context)
        suffix = str(context.cycle)
        facts_id = f"facts-{suffix}"
        risks_id = f"risks-{suffix}"
        return (
            TaskSpec(
                task_id=facts_id,
                description="收集确定性事实",
                capability="facts",
                acceptance_criteria=("输出事实摘要",),
            ),
            TaskSpec(
                task_id=risks_id,
                description="识别交付风险",
                capability="risks",
                acceptance_criteria=("输出风险摘要",),
            ),
            TaskSpec(
                task_id=f"summary-{suffix}",
                description="合并事实与风险",
                capability="synthesis",
                dependencies=(facts_id, risks_id),
                acceptance_criteria=("同时引用两个依赖结果",),
            ),
        )


class HumanThenAcceptReviewer:
    """第一轮请求人工修订，第二轮接受团队结果。"""

    def __init__(self) -> None:
        self.contexts: list[TeamReviewContext] = []

    async def review(self, context: TeamReviewContext) -> TeamReview:
        """根据审查次数返回人工交互或接受结论。

        Args:
            context: 当前 cycle 的草稿、任务结果和历史反馈。

        Returns:
            第一轮 `REQUEST_HUMAN`，后续 `ACCEPT`。
        """
        self.contexts.append(context)
        if len(self.contexts) == 1:
            return TeamReview(
                action=TeamReviewAction.REQUEST_HUMAN,
                feedback="请确认最终结果格式",
                score=85,
                interaction=HumanInteractionRequest(
                    kind=HumanInteractionKind.COMPLETION_REVIEW,
                    prompt="请确认或修订团队草稿",
                    allowed_actions=(
                        HumanAction.APPROVE,
                        HumanAction.REJECT,
                        HumanAction.REVISE,
                        HumanAction.PROVIDE_INPUT,
                    ),
                ),
            )
        return TeamReview(
            action=TeamReviewAction.ACCEPT,
            score=100,
            evidence=("人工意见已进入第二轮规划和任务上下文",),
        )


@dataclass(frozen=True, slots=True)
class TeamExampleResult:
    """多智能体示例的可断言结果摘要。"""

    run_id: str
    status: TeamStatus
    output: str
    cycles: int
    maximum_parallel_tasks: int
    agent_tasks: int
    human_feedback: tuple[str, ...]
    budget_stop_reason: TeamStopReason | None
    event_names: tuple[str, ...]


async def run_team_example() -> TeamExampleResult:
    """运行一次 HITL 团队闭环，并额外验证额度耗尽停止语义。

    Returns:
        团队状态、并发、用量和预算停止原因摘要。
    """
    run_id = "enterprise-team-example"
    ledger = UsageLedger(BudgetLimits(max_agent_tasks=6))
    probe = ConcurrencyProbe()
    planner = ParallelTeamPlanner()
    reviewer = HumanThenAcceptReviewer()
    events = LocalTeamEventPublisher()
    published: list[TeamEvent] = []
    events.subscribe(published.append)
    runtime = _build_runtime(
        planner=planner,
        reviewer=reviewer,
        ledger=ledger,
        probe=probe,
        events=events,
    )

    async with runtime:
        paused = await runtime.run(
            TeamRequest(
                goal="并行分析事实与风险并生成摘要",
                acceptance_criteria=("事实和风险均被验证",),
                limits=TeamLimits(
                    max_tasks=3,
                    max_concurrency=2,
                    max_task_attempts=1,
                    max_cycles=2,
                    max_plan_revisions=1,
                ),
            ),
            run_id=run_id,
        )
        if paused.status is not TeamStatus.PAUSED or paused.pending_interaction is None:
            raise RuntimeError("team example did not reach the expected human pause")
        response = HumanResponse(
            interaction_id=paused.pending_interaction.interaction_id,
            action=HumanAction.REVISE,
            content="最终摘要使用两条短句，并明确事实与风险。",
            idempotency_key="team-revision-1",
        )
        await runtime.submit_human_response(run_id, response)
        await runtime.submit_human_response(run_id, response)
        completed = await runtime.resume(run_id)

    budget_stop_reason = await _run_budget_exhaustion_example()
    usage = ledger.snapshot(f"team:{run_id}")
    return TeamExampleResult(
        run_id=run_id,
        status=completed.status,
        output=completed.output,
        cycles=completed.cycle,
        maximum_parallel_tasks=probe.maximum,
        agent_tasks=usage.agent_tasks,
        human_feedback=tuple(record.response.content for record in completed.human_interactions),
        budget_stop_reason=budget_stop_reason,
        event_names=tuple(event.event_type.value for event in published),
    )


def _build_runtime(
    *,
    planner: ParallelTeamPlanner,
    reviewer: HumanThenAcceptReviewer,
    ledger: UsageLedger,
    probe: ConcurrencyProbe,
    events: LocalTeamEventPublisher,
) -> AsyncTeamRuntime:
    """装配一个所有状态写入都经过 TeamOrchestrator 的运行时。"""
    directory = AgentDirectory()
    analyst = DeterministicAgentEndpoint(
        AgentSpec(
            agent_id="analyst",
            capabilities=frozenset({"facts", "synthesis"}),
            max_concurrency=2,
            role="analyst",
        ),
        probe,
    )
    risk_reviewer = DeterministicAgentEndpoint(
        AgentSpec(
            agent_id="risk-reviewer",
            capabilities=frozenset({"risks"}),
            role="reviewer",
        ),
        probe,
    )
    directory.register(
        BudgetedAgentEndpoint(
            analyst,
            ledger,
            scope_resolver=_agent_usage_scopes,
        )
    )
    directory.register(
        BudgetedAgentEndpoint(
            risk_reviewer,
            ledger,
            scope_resolver=_agent_usage_scopes,
        )
    )
    orchestrator = TeamOrchestrator(
        TeamOrchestratorComponents(
            planner=planner,
            agents=directory,
            selection_policy=LeastBusyScheduler(),
            verifier=ResultSuccessVerifier(),
            approval_gate=AlwaysApproveTeamGate(),
            repository=InMemoryTeamRepository(),
            events=events,
            aggregator=ConcatenateResultAggregator(),
            reviewer=reviewer,
        )
    )
    return AsyncTeamRuntime(orchestrator)


def _agent_usage_scopes(context: AgentTaskContext) -> tuple[str, ...]:
    """把一次 Agent 任务同时归集到 team、task 和 agent scope。"""
    return (
        f"team:{context.team_run_id}",
        f"task:{context.task.task_id}",
        f"agent:{context.agent_id}",
    )


async def _run_budget_exhaustion_example() -> TeamStopReason | None:
    """以单次 Agent 任务上限验证并行预留不会超卖。"""
    ledger = UsageLedger(BudgetLimits(max_agent_tasks=1))
    runtime = _build_runtime(
        planner=ParallelTeamPlanner(),
        reviewer=HumanThenAcceptReviewer(),
        ledger=ledger,
        probe=ConcurrencyProbe(),
        events=LocalTeamEventPublisher(),
    )
    async with runtime:
        result = await runtime.run(
            TeamRequest(
                goal="验证团队额度硬边界",
                limits=TeamLimits(
                    max_tasks=3,
                    max_concurrency=2,
                    max_task_attempts=1,
                    max_cycles=1,
                    max_plan_revisions=0,
                ),
            ),
            run_id="enterprise-team-budget",
        )
    return result.stop_reason


def main() -> None:
    """运行示例并记录不包含任务正文和人工输入的摘要。"""
    logging.basicConfig(level=logging.INFO)
    result = asyncio.run(run_team_example())
    logger.info(
        "团队示例完成",
        extra={
            "run_id": result.run_id,
            "status": result.status.value,
            "cycles": result.cycles,
            "maximum_parallel_tasks": result.maximum_parallel_tasks,
            "agent_tasks": result.agent_tasks,
            "budget_stop_reason": (
                None if result.budget_stop_reason is None else result.budget_stop_reason.value
            ),
        },
    )


if __name__ == "__main__":
    main()
