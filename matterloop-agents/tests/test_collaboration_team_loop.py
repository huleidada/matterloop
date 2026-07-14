"""TeamLoop 外层循环、人工反馈与额度停止语义测试。"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

import pytest
from matterloop_agents.collaboration import (
    AgentDirectory,
    AgentSpec,
    AgentTaskContext,
    AlwaysApproveTeamGate,
    ConcatenateResultAggregator,
    InMemoryTeamRepository,
    LeastBusyScheduler,
    LocalTeamEventPublisher,
    ResultSuccessVerifier,
    TaskResult,
    TaskSpec,
    TeamApprovalGate,
    TeamEvent,
    TeamEventType,
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
    ApprovalDecision,
    HumanAction,
    HumanInteractionKind,
    HumanInteractionNotPendingError,
    HumanInteractionRequest,
    HumanResponse,
    HumanResponseConflictError,
    ResourceLimitExceededError,
)


@dataclass(slots=True)
class _RecordingEndpoint:
    spec: AgentSpec
    successes: tuple[bool, ...] = (True,)
    calls: list[AgentTaskContext] = field(default_factory=list)

    async def execute(self, context: AgentTaskContext) -> TaskResult:
        """按调用序号返回可配置的成功或失败结果。"""
        self.calls.append(context)
        index = min(len(self.calls) - 1, len(self.successes) - 1)
        success = self.successes[index]
        return TaskResult(
            task_id=context.task.task_id,
            agent_id=context.agent_id,
            success=success,
            output=f"cycle={len(self.calls)}" if success else "",
            error="execution failed" if not success else "",
            attempt=context.attempt,
        )


class _BudgetEndpoint(_RecordingEndpoint):
    async def execute(self, context: AgentTaskContext) -> TaskResult:
        """在 Agent 调用边界模拟本地额度用尽。"""
        self.calls.append(context)
        raise ResourceLimitExceededError("test budget exhausted")


class _RecordingPlanner:
    def __init__(
        self,
        *,
        capability: str = "analysis",
        requires_approval: bool = False,
    ) -> None:
        self.capability = capability
        self.requires_approval = requires_approval
        self.contexts: list[TeamPlanningContext] = []

    async def plan(self, context: TeamPlanningContext) -> tuple[TaskSpec, ...]:
        """为每个循环生成不同标识的单任务计划。"""
        self.contexts.append(context)
        return (
            TaskSpec(
                task_id=f"task-{context.cycle}",
                description=f"执行第 {context.cycle} 轮",
                capability=self.capability,
                requires_approval=self.requires_approval,
            ),
        )


class _SequenceReviewer:
    def __init__(self, reviews: tuple[TeamReview, ...]) -> None:
        self._reviews = reviews
        self.contexts: list[TeamReviewContext] = []

    async def review(self, context: TeamReviewContext) -> TeamReview:
        """按调用顺序返回固定团队审查决策。"""
        self.contexts.append(context)
        index = min(len(self.contexts) - 1, len(self._reviews) - 1)
        return self._reviews[index]


def _orchestrator(
    planner: _RecordingPlanner,
    endpoint: _RecordingEndpoint,
    *,
    reviewer: _SequenceReviewer | None = None,
    approval_gate: TeamApprovalGate | None = None,
    events: LocalTeamEventPublisher | None = None,
) -> TeamOrchestrator:
    """用显式内存组件构造可观测的 TeamLoop。"""
    directory = AgentDirectory()
    directory.register(endpoint)
    return TeamOrchestrator(
        TeamOrchestratorComponents(
            planner=planner,
            agents=directory,
            selection_policy=LeastBusyScheduler(),
            verifier=ResultSuccessVerifier(),
            approval_gate=approval_gate or AlwaysApproveTeamGate(),
            repository=InMemoryTeamRepository(),
            events=events or LocalTeamEventPublisher(),
            aggregator=ConcatenateResultAggregator(),
            reviewer=reviewer,
        )
    )


async def test_team_review_replans_with_capability_and_review_history() -> None:
    """团队审查要求重规划时，下一轮必须收到能力和审查历史。"""
    planner = _RecordingPlanner()
    endpoint = _RecordingEndpoint(AgentSpec("analyst", frozenset({"analysis"})))
    reviewer = _SequenceReviewer(
        (
            TeamReview(
                TeamReviewAction.REPLAN,
                feedback="需要更完整的证据",
                score=60,
                failed_criteria=("证据完整",),
            ),
            TeamReview(TeamReviewAction.ACCEPT, score=95, evidence=("已补齐证据",)),
        )
    )

    result = await _orchestrator(planner, endpoint, reviewer=reviewer).run(
        TeamRequest(
            "完成总体分析",
            limits=TeamLimits(max_cycles=2, max_plan_revisions=1),
        )
    )

    assert result.status is TeamStatus.COMPLETED
    assert result.cycle == 2
    assert len(result.cycle_history) == 2
    assert [context.cycle for context in planner.contexts] == [1, 2]
    assert planner.contexts[0].available_capabilities == frozenset({"analysis"})
    assert planner.contexts[1].prior_reviews[0].action is TeamReviewAction.REPLAN
    assert len(endpoint.calls) == 2


async def test_human_revision_is_idempotent_and_reaches_next_planner() -> None:
    """人工修订应稳定去重，并在显式恢复后进入下一轮规划。"""
    planner = _RecordingPlanner()
    endpoint = _RecordingEndpoint(AgentSpec("analyst", frozenset({"analysis"})))
    interaction = HumanInteractionRequest(
        kind=HumanInteractionKind.COMPLETION_REVIEW,
        prompt="是否需要调整团队结果？",
        allowed_actions=(
            HumanAction.APPROVE,
            HumanAction.REJECT,
            HumanAction.REVISE,
            HumanAction.PROVIDE_INPUT,
        ),
    )
    reviewer = _SequenceReviewer(
        (
            TeamReview(
                TeamReviewAction.REQUEST_HUMAN,
                feedback="请确认输出格式",
                score=80,
                interaction=interaction,
            ),
            TeamReview(TeamReviewAction.ACCEPT, score=100),
        )
    )
    events = LocalTeamEventPublisher()
    published: list[TeamEvent] = []
    events.subscribe(published.append)
    orchestrator = _orchestrator(planner, endpoint, reviewer=reviewer, events=events)

    paused = await orchestrator.run(
        TeamRequest(
            "生成可读结果",
            limits=TeamLimits(max_cycles=2, max_plan_revisions=1),
        ),
        run_id="human-revision",
    )
    assert paused.status is TeamStatus.PAUSED
    assert paused.pending_interaction == interaction

    with pytest.raises(HumanInteractionNotPendingError):
        await orchestrator.submit_human_response(
            "human-revision",
            HumanResponse("wrong-interaction", HumanAction.REVISE, "使用列表"),
        )

    response = HumanResponse(
        interaction_id=interaction.interaction_id,
        action=HumanAction.REVISE,
        content="请使用两条简短列表",
        idempotency_key="revision-1",
    )
    submitted = await orchestrator.submit_human_response("human-revision", response)
    duplicate = await orchestrator.submit_human_response("human-revision", response)
    assert submitted.status is TeamStatus.PAUSED
    assert duplicate.human_interactions == submitted.human_interactions

    with pytest.raises(HumanResponseConflictError):
        await orchestrator.submit_human_response(
            "human-revision",
            HumanResponse(
                interaction_id=interaction.interaction_id,
                action=HumanAction.REVISE,
                content="改成三条",
                idempotency_key="revision-1",
            ),
        )

    result = await orchestrator.resume("human-revision")

    assert result.status is TeamStatus.COMPLETED
    assert result.cycle == 2
    assert planner.contexts[1].human_feedback[0].response.content == "请使用两条简短列表"
    assert planner.contexts[1].prior_reviews[0].action is TeamReviewAction.REQUEST_HUMAN
    assert TeamEventType.HUMAN_REVISED in {event.event_type for event in published}


async def test_human_approval_accepts_draft_without_replaying_agent_or_reviewer() -> None:
    """人工批准团队草稿后应精确完成，不重放 Agent 或审查调用。"""
    planner = _RecordingPlanner()
    endpoint = _RecordingEndpoint(AgentSpec("analyst", frozenset({"analysis"})))
    interaction = HumanInteractionRequest(
        HumanInteractionKind.COMPLETION_REVIEW,
        "请批准团队草稿",
        (HumanAction.APPROVE, HumanAction.REJECT),
    )
    reviewer = _SequenceReviewer(
        (
            TeamReview(
                TeamReviewAction.REQUEST_HUMAN,
                score=90,
                interaction=interaction,
            ),
        )
    )
    orchestrator = _orchestrator(planner, endpoint, reviewer=reviewer)

    paused = await orchestrator.run(TeamRequest("交付草稿"), run_id="approve-draft")
    assert paused.pending_interaction == interaction
    await orchestrator.submit_human_response(
        "approve-draft",
        HumanResponse(
            interaction.interaction_id,
            HumanAction.APPROVE,
            idempotency_key="approve-draft-once",
        ),
    )
    result = await orchestrator.resume("approve-draft")

    assert result.status is TeamStatus.COMPLETED
    assert result.output == "cycle=1"
    assert len(endpoint.calls) == 1
    assert len(reviewer.contexts) == 1


async def test_human_rejection_remains_structurally_blocked_on_resume() -> None:
    """人工拒绝是稳定阻塞结果，普通 resume 不得隐式重规划。"""
    planner = _RecordingPlanner()
    endpoint = _RecordingEndpoint(AgentSpec("analyst", frozenset({"analysis"})))
    interaction = HumanInteractionRequest(
        HumanInteractionKind.COMPLETION_REVIEW,
        "请确认是否拒绝草稿",
        (HumanAction.APPROVE, HumanAction.REJECT),
    )
    reviewer = _SequenceReviewer(
        (TeamReview(TeamReviewAction.REQUEST_HUMAN, interaction=interaction),)
    )
    orchestrator = _orchestrator(planner, endpoint, reviewer=reviewer)

    await orchestrator.run(TeamRequest("待拒绝草稿"), run_id="reject-draft")
    rejected = await orchestrator.submit_human_response(
        "reject-draft",
        HumanResponse(
            interaction.interaction_id,
            HumanAction.REJECT,
            "不符合业务目标",
            idempotency_key="reject-once",
        ),
    )
    resumed = await orchestrator.resume("reject-draft")

    assert rejected.status is TeamStatus.BLOCKED
    assert rejected.stop_reason is TeamStopReason.HUMAN_REJECTED
    assert resumed == rejected
    assert len(endpoint.calls) == 1


async def test_task_failures_replan_until_cycle_limit() -> None:
    """任务级重试耗尽后应进入下一循环，而不无界执行。"""
    planner = _RecordingPlanner()
    endpoint = _RecordingEndpoint(
        AgentSpec("analyst", frozenset({"analysis"})),
        successes=(False, False),
    )

    result = await _orchestrator(planner, endpoint).run(
        TeamRequest(
            "无法完成的任务",
            limits=TeamLimits(
                max_task_attempts=1,
                max_cycles=2,
                max_plan_revisions=2,
            ),
        )
    )

    assert result.status is TeamStatus.FAILED
    assert result.stop_reason is TeamStopReason.CYCLE_LIMIT
    assert result.cycle == 2
    assert len(result.cycle_history) == 2
    assert len(planner.contexts) == 2
    assert len(endpoint.calls) == 2


async def test_unknown_planned_capability_is_blocked_before_agent_execution() -> None:
    """规划中的未注册能力必须在调用 Agent 前被拒绝。"""
    planner = _RecordingPlanner(capability="unregistered")
    endpoint = _RecordingEndpoint(AgentSpec("analyst", frozenset({"analysis"})))

    result = await _orchestrator(planner, endpoint).run(TeamRequest("使用未知能力"))

    assert result.status is TeamStatus.BLOCKED
    assert result.stop_reason is TeamStopReason.NO_CAPABLE_AGENT
    assert "unregistered" in result.error
    assert endpoint.calls == []


async def test_agent_budget_exhaustion_blocks_without_retry_or_replan() -> None:
    """额度超限必须直接映射为 BUDGET_EXHAUSTED，不执行无意义重试。"""
    planner = _RecordingPlanner()
    endpoint = _BudgetEndpoint(AgentSpec("analyst", frozenset({"analysis"})))

    result = await _orchestrator(planner, endpoint).run(
        TeamRequest(
            "受额度约束的任务",
            limits=TeamLimits(max_task_attempts=3, max_cycles=3),
        )
    )

    assert result.status is TeamStatus.BLOCKED
    assert result.stop_reason is TeamStopReason.BUDGET_EXHAUSTED
    assert len(endpoint.calls) == 1
    assert len(planner.contexts) == 1


async def test_human_wait_time_does_not_consume_active_timeout() -> None:
    """运行在人工审批期间的墙钟等待不应消耗活跃超时。"""

    class DeferredGate:
        async def decide(self, context: AgentTaskContext) -> ApprovalDecision:
            del context
            return ApprovalDecision.DEFERRED

    planner = _RecordingPlanner(requires_approval=True)
    endpoint = _RecordingEndpoint(AgentSpec("analyst", frozenset({"analysis"})))
    orchestrator = _orchestrator(planner, endpoint, approval_gate=DeferredGate())

    paused = await orchestrator.run(
        TeamRequest("审批后执行", limits=TeamLimits(timeout_seconds=0.05)),
        run_id="paused-timeout",
    )
    assert paused.pending_interaction is not None
    await asyncio.sleep(0.07)
    await orchestrator.submit_human_response(
        "paused-timeout",
        HumanResponse(
            paused.pending_interaction.interaction_id,
            HumanAction.APPROVE,
            idempotency_key="timeout-approve",
        ),
    )
    result = await orchestrator.resume("paused-timeout")

    assert result.status is TeamStatus.COMPLETED
    assert len(endpoint.calls) == 1
