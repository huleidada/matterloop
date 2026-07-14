"""基于任务 DAG、能力路由和独立验证的多 Agent 团队控制器。"""

from __future__ import annotations

import asyncio
from collections.abc import Coroutine
from contextlib import AsyncExitStack, suppress
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from typing import cast
from uuid import uuid4

from matterloop_core import (
    ApprovalDecision,
    HumanAction,
    HumanInteractionKind,
    HumanInteractionNotPendingError,
    HumanInteractionRecord,
    HumanInteractionRequest,
    HumanResponse,
    HumanResponseConflictError,
    ResourceLimitExceededError,
)

from matterloop_agents.collaboration.directory import AgentDirectory
from matterloop_agents.collaboration.errors import (
    AgentCapacityError,
    NoCapableAgentError,
    TeamExecutionError,
    TeamRunActiveError,
    TeamRunNotFoundError,
    TeamStateConflictError,
)
from matterloop_agents.collaboration.events import TeamEvent, TeamEventType
from matterloop_agents.collaboration.models import (
    AgentTaskContext,
    TaskResult,
    TaskSpec,
    TaskState,
    TaskStatus,
    TaskVerification,
    TeamCycleRecord,
    TeamPlanningContext,
    TeamRequest,
    TeamResult,
    TeamReview,
    TeamReviewAction,
    TeamReviewContext,
    TeamSnapshot,
    TeamStatus,
    TeamStopReason,
)
from matterloop_agents.collaboration.protocols import (
    AgentEndpoint,
    AgentSelectionPolicy,
    ResultAggregator,
    TaskVerifier,
    TeamApprovalGate,
    TeamEventPublisher,
    TeamPlanner,
    TeamRepository,
    TeamReviewer,
)
from matterloop_agents.collaboration.task_graph import TaskGraph

_UNSET = object()


@dataclass(frozen=True, slots=True)
class TeamOrchestratorComponents:
    """集中声明团队控制器所需的可替换组件。

    Args:
        planner: 把团队目标拆成任务 DAG 的规划器。
        agents: 提供能力发现、并发租约和热替换的 Agent 目录。
        selection_policy: 从能力匹配的候选中选择 Agent 的策略。
        verifier: 独立验证每个 Agent 任务结果的组件。
        approval_gate: 仅处理显式要求审批任务的审批门。
        repository: 保存可恢复团队快照的仓储。
        events: 接收完整生命周期审计事件的发布器。
        aggregator: 把所有已验证结果汇总成最终输出的组件。
        reviewer: 可选的团队总体目标审查器；未注入时保持 0.1 接受行为。
    """

    planner: TeamPlanner
    agents: AgentDirectory
    selection_policy: AgentSelectionPolicy
    verifier: TaskVerifier
    approval_gate: TeamApprovalGate
    repository: TeamRepository
    events: TeamEventPublisher
    aggregator: ResultAggregator
    reviewer: TeamReviewer | None = None


@dataclass(frozen=True, slots=True)
class _ScheduledTask:
    task: TaskSpec
    endpoint: AgentEndpoint
    context: AgentTaskContext


@dataclass(frozen=True, slots=True)
class _TaskOutcome:
    task: TaskSpec
    context: AgentTaskContext
    result: TaskResult | None = None
    verification: TaskVerification | None = None
    error: str = ""


class _TeamDeadlineExceeded(Exception):
    """仅表示团队控制器自己的总运行时限已经到达。"""


class TeamOrchestrator:
    """协调多个独立 Agent 完成具有依赖关系的团队目标。

    控制器是团队快照的唯一写入者。并行 Agent 只接收隔离上下文并返回 ``TaskResult``，
    所有状态转换、验证结论、重试和下游解锁都在并行调用汇合后顺序提交。

    Args:
        components: 用户显式构造并注入的全部协作组件。
        owner_id: 可选控制器实例标识；用于跨实例运行租约，缺省时随机生成。
    """

    def __init__(
        self,
        components: TeamOrchestratorComponents,
        *,
        owner_id: str | None = None,
    ) -> None:
        self._components = components
        self._owner_id = owner_id or uuid4().hex
        if not self._owner_id.strip():
            raise ValueError("owner_id must not be empty")
        self._active_runs: set[str] = set()
        self._cancel_requested: set[str] = set()

    @staticmethod
    def create_run_id() -> str:
        """创建可供调用方预先关联或取消的团队运行标识。"""
        return uuid4().hex

    async def run(
        self,
        request: TeamRequest,
        *,
        run_id: str | None = None,
    ) -> TeamResult:
        """规划并执行一次新的团队协作运行。

        Args:
            request: 团队目标、验收条件和执行边界。
            run_id: 可选的预生成运行标识。

        Returns:
            完成、暂停、阻塞、失败、取消或超时的结构化结果。

        Raises:
            TeamRunAlreadyExistsError: 运行标识已经被仓储使用。
            ValueError: 显式运行标识为空。
        """
        resolved_run_id = run_id or self.create_run_id()
        if not resolved_run_id.strip():
            raise ValueError("run_id must not be empty")
        timeout = request.limits.timeout_seconds
        deadline = asyncio.get_running_loop().time() + timeout if timeout is not None else None
        started_at = datetime.now(timezone.utc)
        initial = TeamSnapshot(
            request=request,
            tasks=(),
            run_id=resolved_run_id,
            status=TeamStatus.PLANNING,
            active_started_at=started_at,
            created_at=started_at,
            updated_at=started_at,
        )
        await self._components.repository.create(initial)
        await self._claim_run(resolved_run_id)
        try:
            await self._publish(TeamEventType.TEAM_STARTED, initial)
            await self._publish(TeamEventType.PLANNING_STARTED, initial)
            operation = self._plan_and_execute(initial)
            remaining = None if deadline is None else deadline - asyncio.get_running_loop().time()
            if remaining is not None and remaining <= 0:
                operation.close()
                return await self._mark_timed_out(resolved_run_id)
            return await self._await_with_timeout(
                operation,
                remaining,
            )
        except _TeamDeadlineExceeded:
            return await self._mark_timed_out(resolved_run_id)
        except asyncio.CancelledError:
            with suppress(Exception):
                await asyncio.shield(self._mark_cancelled(resolved_run_id))
            raise
        except ResourceLimitExceededError:
            return await self._mark_budget_exhausted(resolved_run_id)
        except TeamStateConflictError as exc:
            return await self._resolve_conflict(resolved_run_id, exc)
        except Exception as exc:
            return await self._mark_component_failed(resolved_run_id, exc)
        finally:
            self._finish_active(resolved_run_id)
            await self._components.repository.release_lease(
                resolved_run_id,
                self._owner_id,
            )

    async def resume(self, run_id: str) -> TeamResult:
        """从持久化快照恢复暂停或阻塞的团队运行。

        Args:
            run_id: 需要恢复的团队运行标识。

        Returns:
            恢复执行后的最新团队结果。

        Raises:
            TeamRunNotFoundError: 仓储中不存在指定运行。
            TeamExecutionError: 运行不是可恢复状态或已经在执行。
        """
        snapshot = await self._require(run_id)
        if snapshot.status.is_terminal:
            return self._result(snapshot)
        if (
            snapshot.status is TeamStatus.BLOCKED
            and snapshot.stop_reason is TeamStopReason.HUMAN_REJECTED
        ):
            return self._result(snapshot)
        if snapshot.pending_interaction is not None:
            return self._result(snapshot)
        if snapshot.status not in {
            TeamStatus.CREATED,
            TeamStatus.PLANNING,
            TeamStatus.RUNNING,
            TeamStatus.PAUSED,
            TeamStatus.BLOCKED,
            TeamStatus.WAITING_APPROVAL,
        }:
            raise TeamExecutionError(f"team run cannot resume from status: {snapshot.status.value}")
        await self._claim_run(run_id)
        try:
            if snapshot.tasks:
                graph = TaskGraph.from_snapshot(snapshot)
                graph.recover_inflight()
                snapshot = await self._save(
                    snapshot,
                    graph,
                    status=TeamStatus.RUNNING,
                    stop_reason=None,
                    error="",
                    cycle=max(snapshot.cycle, 1),
                )
                operation = self._execute(snapshot, graph)
            else:
                snapshot = await self._save(
                    snapshot,
                    None,
                    status=TeamStatus.PLANNING,
                    stop_reason=None,
                    error="",
                )
                operation = self._plan_and_execute(snapshot)
            await self._publish(TeamEventType.TEAM_RESUMED, snapshot)
            remaining = self._remaining_timeout(snapshot)
            if remaining is None:
                return await operation
            if remaining <= 0:
                operation.close()
                return await self._mark_timed_out(run_id)
            return await self._await_with_timeout(operation, remaining)
        except _TeamDeadlineExceeded:
            return await self._mark_timed_out(run_id)
        except asyncio.CancelledError:
            with suppress(Exception):
                await asyncio.shield(self._mark_cancelled(run_id))
            raise
        except ResourceLimitExceededError:
            return await self._mark_budget_exhausted(run_id)
        except TeamStateConflictError as exc:
            return await self._resolve_conflict(run_id, exc)
        except Exception as exc:
            return await self._mark_component_failed(run_id, exc)
        finally:
            self._finish_active(run_id)
            await self._components.repository.release_lease(run_id, self._owner_id)

    async def cancel(self, run_id: str) -> bool:
        """请求在并行批次的安全边界取消团队运行。

        Args:
            run_id: 目标团队运行标识。

        Returns:
            是否接受了新的取消请求；不存在或已经终止时返回 ``False``。
        """
        snapshot = await self._components.repository.load(run_id)
        if snapshot is None or snapshot.status.is_terminal:
            return False
        if run_id in self._active_runs:
            if run_id in self._cancel_requested:
                return False
            self._cancel_requested.add(run_id)
            return True
        graph = TaskGraph.from_snapshot(snapshot) if snapshot.tasks else None
        if graph is not None:
            graph.cancel_all()
        snapshot = await self._save(
            snapshot,
            graph,
            status=TeamStatus.CANCELLED,
            stop_reason=TeamStopReason.CANCELLED,
            error="team run was cancelled",
        )
        await self._publish(TeamEventType.TEAM_CANCELLED, snapshot)
        return True

    async def get(self, run_id: str) -> TeamResult:
        """读取一次团队运行的最新公开结果。

        Args:
            run_id: 团队运行标识。

        Returns:
            从最新持久化快照构造的结果。
        """
        return self._result(await self._require(run_id))

    async def list(self) -> tuple[TeamSnapshot, ...]:
        """返回仓储中稳定排序的团队运行快照。"""
        return await self._components.repository.list()

    async def submit_human_response(
        self,
        run_id: str,
        response: HumanResponse,
    ) -> TeamResult:
        """原子提交团队人工反馈，但不隐式恢复执行。

        Args:
            run_id: 正在等待人工响应的团队运行标识。
            response: 包含交互标识和幂等键的结构化响应。

        Returns:
            已持久化反馈的最新团队结果。

        Raises:
            HumanInteractionNotPendingError: 当前没有匹配的待处理交互。
            HumanResponseConflictError: 同一幂等键被用于不同响应。
        """
        try:
            return await self._submit_human_response_once(run_id, response)
        except TeamStateConflictError:
            # 并发提交发生 CAS 竞争时，只对业务内容相同的幂等响应收敛为 no-op。
            latest = await self._require(run_id)
            for record in latest.human_interactions:
                previous = record.response
                if previous.idempotency_key != response.idempotency_key:
                    continue
                if self._same_human_response(previous, response):
                    return self._result(latest)
                raise HumanResponseConflictError(
                    "human response idempotency key contains conflicting content"
                ) from None
            raise

    async def _submit_human_response_once(
        self,
        run_id: str,
        response: HumanResponse,
    ) -> TeamResult:
        snapshot = await self._require(run_id)
        for record in snapshot.human_interactions:
            previous = record.response
            if previous.idempotency_key != response.idempotency_key:
                continue
            if self._same_human_response(previous, response):
                return self._result(snapshot)
            raise HumanResponseConflictError(
                "human response idempotency key contains conflicting content"
            )

        pending = snapshot.pending_interaction
        if pending is None or pending.interaction_id != response.interaction_id:
            raise HumanInteractionNotPendingError(
                "human response does not match the pending team interaction"
            )
        record = HumanInteractionRecord(request=pending, response=response)
        interactions = (*snapshot.human_interactions, record)
        graph = TaskGraph.from_snapshot(snapshot) if snapshot.tasks else None
        task_value = pending.metadata.get("task_id")
        task_id = task_value if isinstance(task_value, str) else None

        if response.action is HumanAction.REJECT:
            if graph is not None:
                if (
                    task_id is not None
                    and graph.state(task_id).status is TaskStatus.WAITING_APPROVAL
                ):
                    graph.fail(task_id, "human rejected the task")
                graph.cancel_all()
            snapshot = await self._save(
                snapshot,
                graph,
                status=TeamStatus.BLOCKED,
                stop_reason=TeamStopReason.HUMAN_REJECTED,
                error=response.content or "human rejected the team run",
                pending_interaction=None,
                pending_review=None,
                human_interactions=interactions,
            )
            await self._publish(TeamEventType.HUMAN_RESPONSE_SUBMITTED, snapshot)
            await self._publish(TeamEventType.HUMAN_REJECTED, snapshot)
            return self._result(snapshot)

        if response.action is HumanAction.APPROVE:
            approved_review_cycle = snapshot.review_approved_cycle
            if graph is not None and task_id is not None:
                state = graph.state(task_id)
                if state.status is not TaskStatus.WAITING_APPROVAL:
                    raise HumanInteractionNotPendingError(
                        "pending approval task is no longer waiting"
                    )
                graph.resume_approval(task_id)
            else:
                approved_review_cycle = snapshot.cycle
            snapshot = await self._save(
                snapshot,
                graph,
                status=TeamStatus.PAUSED,
                stop_reason=None,
                error="",
                pending_interaction=None,
                human_interactions=interactions,
                review_approved_cycle=approved_review_cycle,
            )
            await self._publish(TeamEventType.HUMAN_RESPONSE_SUBMITTED, snapshot)
            await self._publish(TeamEventType.HUMAN_APPROVED, snapshot)
            return self._result(snapshot)

        if response.action not in {HumanAction.REVISE, HumanAction.PROVIDE_INPUT}:
            raise HumanResponseConflictError("unsupported human action for team interaction")
        if response.action is HumanAction.REVISE and not response.content.strip():
            raise HumanResponseConflictError("REVISE human response requires non-empty content")
        history = snapshot.cycle_history
        if graph is not None:
            history = (
                *history,
                TeamCycleRecord(
                    cycle=max(snapshot.cycle, 1),
                    plan_revision=snapshot.plan_revision,
                    tasks=graph.states(),
                    draft_output=snapshot.output,
                    review=snapshot.pending_review,
                    error=response.content,
                ),
            )
        if snapshot.plan_revision >= snapshot.request.limits.max_plan_revisions:
            snapshot = await self._save(
                snapshot,
                graph,
                status=TeamStatus.FAILED,
                stop_reason=TeamStopReason.PLAN_REVISION_LIMIT,
                error="team plan revision limit was exhausted",
                pending_interaction=None,
                pending_review=None,
                human_interactions=interactions,
                cycle_history=history,
            )
            return self._result(snapshot)
        snapshot = await self._save(
            snapshot,
            graph,
            status=TeamStatus.PAUSED,
            stop_reason=None,
            output="",
            error="",
            tasks_override=(),
            pending_interaction=None,
            pending_review=None,
            human_interactions=interactions,
            cycle_history=history,
            plan_revision=snapshot.plan_revision + 1,
            review_approved_cycle=None,
        )
        await self._publish(TeamEventType.HUMAN_RESPONSE_SUBMITTED, snapshot)
        event_type = (
            TeamEventType.HUMAN_REVISED
            if response.action is HumanAction.REVISE
            else TeamEventType.HUMAN_INPUT_PROVIDED
        )
        await self._publish(event_type, snapshot)
        return self._result(snapshot)

    async def _plan_and_execute(self, snapshot: TeamSnapshot) -> TeamResult:
        next_cycle = snapshot.cycle + 1
        if next_cycle > snapshot.request.limits.max_cycles:
            snapshot = await self._save(
                snapshot,
                None,
                status=TeamStatus.FAILED,
                stop_reason=TeamStopReason.CYCLE_LIMIT,
                error="team cycle limit was exhausted",
            )
            await self._publish(TeamEventType.TEAM_FAILED, snapshot, detail=snapshot.error)
            return self._result(snapshot)
        prior_reviews = tuple(
            record.review for record in snapshot.cycle_history if record.review is not None
        )
        planning_context = TeamPlanningContext(
            run_id=snapshot.run_id,
            request=snapshot.request,
            cycle=next_cycle,
            plan_revision=snapshot.plan_revision,
            available_agents=self._components.agents.candidates(),
            prior_reviews=prior_reviews,
            human_feedback=snapshot.human_interactions,
        )
        tasks = await self._components.planner.plan(planning_context)
        if len(tasks) > snapshot.request.limits.max_tasks:
            raise TeamExecutionError(
                f"planner returned {len(tasks)} tasks; limit is {snapshot.request.limits.max_tasks}"
            )
        graph = TaskGraph(tasks)
        snapshot = await self._save(
            snapshot,
            graph,
            status=TeamStatus.RUNNING,
            stop_reason=None,
            error="",
            cycle=next_cycle,
            review_approved_cycle=None,
        )
        await self._publish(
            TeamEventType.PLAN_CREATED,
            snapshot,
            metadata={"task_count": len(tasks)},
        )
        available = planning_context.available_capabilities
        unknown = tuple(
            sorted({task.capability for task in tasks if task.capability not in available})
        )
        if unknown:
            snapshot = await self._block(
                snapshot,
                graph,
                TeamStopReason.NO_CAPABLE_AGENT,
                f"plan references unregistered capabilities: {', '.join(unknown)}",
            )
            return self._result(snapshot)
        await self._publish_ready_tasks(snapshot, graph.ready_tasks())
        return await self._execute(snapshot, graph)

    async def _execute(self, snapshot: TeamSnapshot, graph: TaskGraph) -> TeamResult:
        while True:
            if snapshot.run_id in self._cancel_requested:
                return await self._cancel_snapshot(snapshot, graph)
            if any(state.status is TaskStatus.VERIFYING for state in graph.states()):
                snapshot = await self._resume_verifying(snapshot, graph)
                continue
            if graph.all_succeeded:
                return await self._complete(snapshot, graph)
            if graph.is_terminal:
                return await self._fail_tasks(snapshot, graph)

            snapshot, should_continue = await self._approve_ready(snapshot, graph)
            if not should_continue:
                return self._result(snapshot)

            ready = graph.ready_tasks()
            if not ready:
                snapshot = await self._block(
                    snapshot,
                    graph,
                    TeamStopReason.DEADLOCK,
                    "task graph has no ready task and is not terminal",
                )
                return self._result(snapshot)
            snapshot = await self._execute_batch(snapshot, graph, ready)
            if snapshot.status in {TeamStatus.BLOCKED, TeamStatus.FAILED}:
                return self._result(snapshot)

    async def _resume_verifying(
        self,
        snapshot: TeamSnapshot,
        graph: TaskGraph,
    ) -> TeamSnapshot:
        """从持久化执行结果继续验证，避免重复执行有副作用的端点。"""
        for state in graph.states():
            if state.status is not TaskStatus.VERIFYING:
                continue
            result = state.result
            assigned_agent = state.assigned_agent
            if result is None or assigned_agent is None:
                graph.retry(state.spec.task_id, "verifying task lost its execution result")
                snapshot = await self._save(snapshot, graph, status=TeamStatus.RUNNING)
                continue
            context = AgentTaskContext(
                team_run_id=snapshot.run_id,
                request=snapshot.request,
                task=state.spec,
                agent_id=assigned_agent,
                attempt=state.attempt,
                dependency_results=graph.dependency_results(state.spec.task_id),
                previous_error=state.error,
                human_feedback=snapshot.human_interactions,
            )
            outcome = _TaskOutcome(
                state.spec,
                context,
                result=result,
                verification=state.verification,
            )
            if outcome.verification is None:
                outcome = await self._verify(outcome)
            snapshot = await self._apply_outcome(snapshot, graph, outcome)
        return snapshot

    async def _approve_ready(
        self,
        snapshot: TeamSnapshot,
        graph: TaskGraph,
    ) -> tuple[TeamSnapshot, bool]:
        for task in graph.ready_tasks():
            state = graph.state(task.task_id)
            if not task.requires_approval or state.approval_granted:
                continue
            graph.mark_waiting_approval(task.task_id)
            snapshot = await self._save(
                snapshot,
                graph,
                status=TeamStatus.WAITING_APPROVAL,
                stop_reason=None,
                error="",
            )
            await self._publish(
                TeamEventType.APPROVAL_REQUESTED,
                snapshot,
                metadata={"task_id": task.task_id},
            )
            context = AgentTaskContext(
                team_run_id=snapshot.run_id,
                request=snapshot.request,
                task=task,
                agent_id="unassigned",
                attempt=state.attempt + 1,
                dependency_results=graph.dependency_results(task.task_id),
                previous_error=state.error,
                human_feedback=snapshot.human_interactions,
            )
            decision = await self._components.approval_gate.decide(context)
            if decision is ApprovalDecision.APPROVED:
                graph.resume_approval(task.task_id)
                snapshot = await self._save(
                    snapshot,
                    graph,
                    status=TeamStatus.RUNNING,
                    stop_reason=None,
                    error="",
                )
                await self._publish(
                    TeamEventType.APPROVAL_GRANTED,
                    snapshot,
                    metadata={"task_id": task.task_id},
                )
                await self._publish_ready_tasks(snapshot, (task,))
                continue
            if decision is ApprovalDecision.DEFERRED:
                interaction = HumanInteractionRequest(
                    kind=HumanInteractionKind.APPROVAL,
                    prompt=f"是否批准执行团队任务：{task.description}",
                    allowed_actions=(
                        HumanAction.APPROVE,
                        HumanAction.REJECT,
                        HumanAction.REVISE,
                        HumanAction.PROVIDE_INPUT,
                    ),
                    step_id=task.task_id,
                    metadata={
                        "team_run_id": snapshot.run_id,
                        "task_id": task.task_id,
                        "source": "team_approval_gate",
                    },
                )
                snapshot = await self._save(
                    snapshot,
                    graph,
                    status=TeamStatus.PAUSED,
                    stop_reason=TeamStopReason.APPROVAL_DEFERRED,
                    error="task approval was deferred",
                    pending_interaction=interaction,
                )
                await self._publish(
                    TeamEventType.HUMAN_INTERACTION_REQUESTED,
                    snapshot,
                    metadata={
                        "interaction_id": interaction.interaction_id,
                        "task_id": task.task_id,
                    },
                )
                await self._publish(
                    TeamEventType.TEAM_PAUSED,
                    snapshot,
                    metadata={"task_id": task.task_id},
                )
                return snapshot, False
            if decision is not ApprovalDecision.REJECTED:
                raise TeamExecutionError(
                    f"approval gate returned an invalid decision: {decision!r}"
                )
            graph.fail(task.task_id, "task approval was rejected")
            graph.cancel_all()
            snapshot = await self._save(
                snapshot,
                graph,
                status=TeamStatus.FAILED,
                stop_reason=TeamStopReason.APPROVAL_REJECTED,
                error="task approval was rejected",
            )
            await self._publish(
                TeamEventType.APPROVAL_REJECTED,
                snapshot,
                metadata={"task_id": task.task_id},
            )
            await self._publish(
                TeamEventType.TEAM_FAILED,
                snapshot,
                metadata={"task_id": task.task_id},
            )
            return snapshot, False
        return snapshot, True

    async def _execute_batch(
        self,
        snapshot: TeamSnapshot,
        graph: TaskGraph,
        ready: tuple[TaskSpec, ...],
    ) -> TeamSnapshot:
        limit = snapshot.request.limits.max_concurrency
        capacity_error: AgentCapacityError | None = None
        capability_error: NoCapableAgentError | None = None
        async with AsyncExitStack() as leases:
            scheduled: list[_ScheduledTask] = []
            for task in ready:
                if len(scheduled) >= limit:
                    break
                state = graph.state(task.task_id)
                try:
                    lease = await leases.enter_async_context(
                        self._components.agents.acquire(
                            task,
                            self._components.selection_policy,
                        )
                    )
                except AgentCapacityError as exc:
                    capacity_error = exc
                    continue
                except NoCapableAgentError as exc:
                    capability_error = exc
                    continue
                started = graph.start(task.task_id, lease.spec.agent_id)
                context = AgentTaskContext(
                    team_run_id=snapshot.run_id,
                    request=snapshot.request,
                    task=task,
                    agent_id=lease.spec.agent_id,
                    attempt=started.attempt,
                    dependency_results=graph.dependency_results(task.task_id),
                    previous_error=state.error,
                    human_feedback=snapshot.human_interactions,
                )
                scheduled.append(_ScheduledTask(task, lease.endpoint, context))

            if not scheduled:
                if capability_error is not None:
                    return await self._block(
                        snapshot,
                        graph,
                        TeamStopReason.NO_CAPABLE_AGENT,
                        str(capability_error),
                    )
                if capacity_error is not None:
                    return await self._block(
                        snapshot,
                        graph,
                        TeamStopReason.AGENT_CAPACITY,
                        str(capacity_error),
                    )
                raise TeamExecutionError("scheduler did not assign any ready task")

            snapshot = await self._save(
                snapshot,
                graph,
                status=TeamStatus.RUNNING,
                stop_reason=None,
                error="",
            )
            for item in scheduled:
                metadata = {
                    "task_id": item.task.task_id,
                    "agent_id": item.context.agent_id,
                    "attempt": item.context.attempt,
                }
                await self._publish(TeamEventType.TASK_ASSIGNED, snapshot, metadata=metadata)
                await self._publish(TeamEventType.TASK_STARTED, snapshot, metadata=metadata)
            outcomes = await asyncio.gather(
                *(self._invoke(item) for item in scheduled),
            )

        verifying = tuple(
            outcome for outcome in outcomes if outcome.result is not None and outcome.result.success
        )
        if verifying:
            for outcome in verifying:
                graph.begin_verification(
                    outcome.task.task_id,
                    outcome.result,
                )
            snapshot = await self._save(snapshot, graph, status=TeamStatus.RUNNING)
            for outcome in verifying:
                await self._publish(
                    TeamEventType.TASK_VERIFYING,
                    snapshot,
                    metadata={
                        "task_id": outcome.task.task_id,
                        "agent_id": outcome.context.agent_id,
                        "attempt": outcome.context.attempt,
                    },
                )
        outcomes = await asyncio.gather(*(self._verify(outcome) for outcome in outcomes))
        for outcome in outcomes:
            snapshot = await self._apply_outcome(snapshot, graph, outcome)
        return snapshot

    async def _invoke(self, scheduled: _ScheduledTask) -> _TaskOutcome:
        try:
            result = await scheduled.endpoint.execute(scheduled.context)
            self._validate_result(scheduled, result)
            if not result.success:
                return _TaskOutcome(
                    scheduled.task,
                    scheduled.context,
                    result=result,
                    error=result.error or "agent reported an unsuccessful task result",
                )
            return _TaskOutcome(scheduled.task, scheduled.context, result=result)
        except asyncio.CancelledError:
            raise
        except ResourceLimitExceededError:
            raise
        except Exception as exc:
            return _TaskOutcome(
                scheduled.task,
                scheduled.context,
                error=f"{type(exc).__name__}: {exc}",
            )

    async def _verify(self, outcome: _TaskOutcome) -> _TaskOutcome:
        result = outcome.result
        if result is None or not result.success:
            return outcome
        try:
            verification = await self._components.verifier.verify(
                outcome.context,
                result,
            )
            return _TaskOutcome(
                outcome.task,
                outcome.context,
                result=result,
                verification=verification,
                error=(
                    "" if verification.passed else (verification.feedback or "verification failed")
                ),
            )
        except asyncio.CancelledError:
            raise
        except ResourceLimitExceededError:
            raise
        except Exception as exc:
            return _TaskOutcome(
                outcome.task,
                outcome.context,
                result=result,
                error=f"{type(exc).__name__}: {exc}",
            )

    async def _apply_outcome(
        self,
        snapshot: TeamSnapshot,
        graph: TaskGraph,
        outcome: _TaskOutcome,
    ) -> TeamSnapshot:
        task_id = outcome.task.task_id
        if outcome.verification is not None:
            graph.record_verification(task_id, outcome.verification)
            snapshot = await self._save(snapshot, graph, status=TeamStatus.RUNNING)
            await self._publish(
                TeamEventType.TASK_VERIFIED,
                snapshot,
                metadata={
                    "task_id": task_id,
                    "passed": outcome.verification.passed,
                    "score": outcome.verification.score,
                    "evidence": outcome.verification.evidence,
                    "failed_criteria": outcome.verification.failed_criteria,
                },
            )
        verification_passed = outcome.verification is not None and outcome.verification.passed
        if verification_passed and outcome.result is not None:
            previously_ready = {task.task_id for task in graph.ready_tasks()}
            graph.succeed(task_id, outcome.result)
            snapshot = await self._save(snapshot, graph, status=TeamStatus.RUNNING)
            await self._publish(
                TeamEventType.TASK_COMPLETED,
                snapshot,
                metadata={
                    "task_id": task_id,
                    "agent_id": outcome.context.agent_id,
                    "attempt": outcome.context.attempt,
                },
            )
            newly_ready = tuple(
                task for task in graph.ready_tasks() if task.task_id not in previously_ready
            )
            await self._publish_ready_tasks(snapshot, newly_ready)
            return snapshot

        state = graph.state(task_id)
        error = outcome.error or "task did not pass verification"
        if state.attempt < snapshot.request.limits.max_task_attempts:
            graph.retry(
                task_id,
                error,
                result=outcome.result,
                verification=outcome.verification,
            )
            snapshot = await self._save(snapshot, graph, status=TeamStatus.RUNNING)
            await self._publish(
                TeamEventType.TASK_RETRYING,
                snapshot,
                detail=error,
                metadata={"task_id": task_id, "attempt": state.attempt},
            )
            await self._publish_ready_tasks(snapshot, (outcome.task,))
            return snapshot

        graph.fail(
            task_id,
            error,
            result=outcome.result,
            verification=outcome.verification,
        )
        snapshot = await self._save(
            snapshot,
            graph,
            status=TeamStatus.RUNNING,
            error=error,
        )
        await self._publish(
            TeamEventType.TASK_FAILED,
            snapshot,
            detail=error,
            metadata={"task_id": task_id, "attempt": state.attempt},
        )
        return snapshot

    async def _complete(self, snapshot: TeamSnapshot, graph: TaskGraph) -> TeamResult:
        if snapshot.run_id in self._cancel_requested:
            return await self._cancel_snapshot(snapshot, graph)
        results = graph.successful_results()
        human_approved = snapshot.review_approved_cycle == snapshot.cycle
        output = (
            snapshot.output
            if human_approved and snapshot.output
            else await self._components.aggregator.aggregate(snapshot.request, results)
        )
        if snapshot.run_id in self._cancel_requested:
            return await self._cancel_snapshot(snapshot, graph)
        prior_reviews = tuple(
            record.review for record in snapshot.cycle_history if record.review is not None
        )
        review_context = TeamReviewContext(
            run_id=snapshot.run_id,
            request=snapshot.request,
            cycle=snapshot.cycle,
            plan_revision=snapshot.plan_revision,
            task_results=results,
            draft_output=output,
            prior_reviews=prior_reviews,
            human_feedback=snapshot.human_interactions,
        )
        await self._publish(TeamEventType.REVIEW_STARTED, snapshot)
        if human_approved or self._components.reviewer is None:
            review = TeamReview(
                action=TeamReviewAction.ACCEPT,
                feedback="human approved the team draft" if human_approved else "",
                score=100.0,
            )
        else:
            review = await self._components.reviewer.review(review_context)
        await self._publish(
            TeamEventType.REVIEW_COMPLETED,
            snapshot,
            metadata={
                "action": review.action.value,
                "score": review.score,
                "failed_criteria": review.failed_criteria,
            },
        )
        if review.action is TeamReviewAction.REPLAN:
            return await self._replan(
                snapshot,
                graph,
                output=output,
                review=review,
                error=review.feedback or "team review requested replanning",
            )
        if review.action is TeamReviewAction.REQUEST_HUMAN:
            interaction = review.interaction
            if interaction is None:
                raise TeamExecutionError("team reviewer omitted the human interaction")
            snapshot = await self._save(
                snapshot,
                graph,
                status=TeamStatus.PAUSED,
                stop_reason=TeamStopReason.APPROVAL_DEFERRED,
                output=output,
                error=review.feedback,
                pending_interaction=interaction,
                pending_review=review,
            )
            await self._publish(
                TeamEventType.HUMAN_INTERACTION_REQUESTED,
                snapshot,
                metadata={"interaction_id": interaction.interaction_id},
            )
            await self._publish(TeamEventType.TEAM_PAUSED, snapshot)
            return self._result(snapshot)
        history = (
            *snapshot.cycle_history,
            TeamCycleRecord(
                cycle=snapshot.cycle,
                plan_revision=snapshot.plan_revision,
                tasks=graph.states(),
                draft_output=output,
                review=review,
                error=review.feedback if review.action is TeamReviewAction.STOP else "",
            ),
        )
        if review.action is TeamReviewAction.STOP:
            snapshot = await self._save(
                snapshot,
                graph,
                status=TeamStatus.FAILED,
                stop_reason=TeamStopReason.REVIEW_STOPPED,
                output=output,
                error=review.feedback or "team reviewer stopped the run",
                cycle_history=history,
            )
            await self._publish(TeamEventType.TEAM_FAILED, snapshot, detail=snapshot.error)
            return self._result(snapshot)
        if review.action is not TeamReviewAction.ACCEPT:
            raise TeamExecutionError(f"unsupported team review action: {review.action!r}")
        snapshot = await self._save(
            snapshot,
            graph,
            status=TeamStatus.COMPLETED,
            stop_reason=TeamStopReason.COMPLETED,
            output=output,
            error="",
            cycle_history=history,
            pending_interaction=None,
            pending_review=None,
        )
        await self._publish(TeamEventType.TEAM_COMPLETED, snapshot)
        return self._result(snapshot)

    async def _fail_tasks(self, snapshot: TeamSnapshot, graph: TaskGraph) -> TeamResult:
        errors = tuple(state.error for state in graph.states() if state.error)
        error = "; ".join(errors) or "one or more tasks did not complete successfully"
        return await self._replan(snapshot, graph, error=error)

    async def _replan(
        self,
        snapshot: TeamSnapshot,
        graph: TaskGraph,
        *,
        output: str = "",
        review: TeamReview | None = None,
        error: str,
    ) -> TeamResult:
        """归档当前循环，并在循环与修订硬边界内重新规划。"""
        history = (
            *snapshot.cycle_history,
            TeamCycleRecord(
                cycle=snapshot.cycle,
                plan_revision=snapshot.plan_revision,
                tasks=graph.states(),
                draft_output=output,
                review=review,
                error=error,
            ),
        )
        if snapshot.cycle >= snapshot.request.limits.max_cycles:
            snapshot = await self._save(
                snapshot,
                graph,
                status=TeamStatus.FAILED,
                stop_reason=TeamStopReason.CYCLE_LIMIT,
                output=output,
                error=error,
                cycle_history=history,
            )
            await self._publish(TeamEventType.TEAM_FAILED, snapshot, detail=error)
            return self._result(snapshot)
        if snapshot.plan_revision >= snapshot.request.limits.max_plan_revisions:
            snapshot = await self._save(
                snapshot,
                graph,
                status=TeamStatus.FAILED,
                stop_reason=TeamStopReason.PLAN_REVISION_LIMIT,
                output=output,
                error=error,
                cycle_history=history,
            )
            await self._publish(TeamEventType.TEAM_FAILED, snapshot, detail=error)
            return self._result(snapshot)
        snapshot = await self._save(
            snapshot,
            graph,
            status=TeamStatus.PLANNING,
            stop_reason=None,
            output="",
            error="",
            tasks_override=(),
            cycle_history=history,
            plan_revision=snapshot.plan_revision + 1,
            review_approved_cycle=None,
        )
        await self._publish(
            TeamEventType.REPLAN_REQUESTED,
            snapshot,
            detail=error,
            metadata={"next_cycle": snapshot.cycle + 1},
        )
        await self._publish(TeamEventType.PLANNING_STARTED, snapshot)
        return await self._plan_and_execute(snapshot)

    async def _block(
        self,
        snapshot: TeamSnapshot,
        graph: TaskGraph,
        reason: TeamStopReason,
        error: str,
    ) -> TeamSnapshot:
        snapshot = await self._save(
            snapshot,
            graph,
            status=TeamStatus.BLOCKED,
            stop_reason=reason,
            error=error,
        )
        await self._publish(TeamEventType.TEAM_BLOCKED, snapshot, detail=error)
        return snapshot

    async def _cancel_snapshot(
        self,
        snapshot: TeamSnapshot,
        graph: TaskGraph,
    ) -> TeamResult:
        graph.cancel_all()
        snapshot = await self._save(
            snapshot,
            graph,
            status=TeamStatus.CANCELLED,
            stop_reason=TeamStopReason.CANCELLED,
            error="team run was cancelled",
        )
        await self._publish(TeamEventType.TEAM_CANCELLED, snapshot)
        return self._result(snapshot)

    async def _mark_timed_out(self, run_id: str) -> TeamResult:
        snapshot = await self._require(run_id)
        if snapshot.status.is_terminal:
            return self._result(snapshot)
        graph = TaskGraph.from_snapshot(snapshot) if snapshot.tasks else None
        if graph is not None:
            graph.cancel_all()
        snapshot = await self._save(
            snapshot,
            graph,
            status=TeamStatus.TIMED_OUT,
            stop_reason=TeamStopReason.TIMED_OUT,
            error="team run exceeded its timeout",
        )
        with suppress(Exception):
            await self._publish(TeamEventType.TEAM_TIMED_OUT, snapshot)
        return self._result(snapshot)

    async def _mark_budget_exhausted(self, run_id: str) -> TeamResult:
        """将任意组件抛出的本地额度超限映射为不可重试阻塞状态。"""
        snapshot = await self._require(run_id)
        if snapshot.status.is_terminal:
            return self._result(snapshot)
        graph = TaskGraph.from_snapshot(snapshot) if snapshot.tasks else None
        snapshot = await self._save(
            snapshot,
            graph,
            status=TeamStatus.BLOCKED,
            stop_reason=TeamStopReason.BUDGET_EXHAUSTED,
            error="team resource budget was exhausted",
        )
        with suppress(Exception):
            await self._publish(TeamEventType.TEAM_BLOCKED, snapshot, detail=snapshot.error)
        return self._result(snapshot)

    async def _mark_cancelled(self, run_id: str) -> TeamResult:
        """在调用协程被外部取消时提交一致的终态快照。"""
        snapshot = await self._require(run_id)
        if snapshot.status.is_terminal:
            return self._result(snapshot)
        graph = TaskGraph.from_snapshot(snapshot) if snapshot.tasks else None
        if graph is not None:
            graph.cancel_all()
        snapshot = await self._save(
            snapshot,
            graph,
            status=TeamStatus.CANCELLED,
            stop_reason=TeamStopReason.CANCELLED,
            error="team run coroutine was cancelled",
        )
        with suppress(Exception):
            await self._publish(TeamEventType.TEAM_CANCELLED, snapshot)
        return self._result(snapshot)

    async def _mark_component_failed(self, run_id: str, exc: Exception) -> TeamResult:
        snapshot = await self._require(run_id)
        if snapshot.status.is_terminal:
            return self._result(snapshot)
        error = f"{type(exc).__name__}: {exc}"
        graph = TaskGraph.from_snapshot(snapshot) if snapshot.tasks else None
        if graph is not None:
            graph.cancel_all()
        snapshot = await self._save(
            snapshot,
            graph,
            status=TeamStatus.FAILED,
            stop_reason=TeamStopReason.COMPONENT_ERROR,
            error=error,
        )
        with suppress(Exception):
            await self._publish(TeamEventType.TEAM_FAILED, snapshot, detail=error)
        return self._result(snapshot)

    async def _save(
        self,
        snapshot: TeamSnapshot,
        graph: TaskGraph | None,
        *,
        status: TeamStatus,
        stop_reason: TeamStopReason | None = None,
        output: str = "",
        error: str = "",
        tasks_override: tuple[TaskState, ...] | None = None,
        cycle: int | object = _UNSET,
        plan_revision: int | object = _UNSET,
        cycle_history: tuple[TeamCycleRecord, ...] | object = _UNSET,
        pending_interaction: HumanInteractionRequest | None | object = _UNSET,
        pending_review: TeamReview | None | object = _UNSET,
        human_interactions: tuple[HumanInteractionRecord, ...] | object = _UNSET,
        review_approved_cycle: int | None | object = _UNSET,
    ) -> TeamSnapshot:
        now = datetime.now(timezone.utc)
        active_elapsed = snapshot.active_elapsed_seconds
        active_started = snapshot.active_started_at
        inactive = (
            status
            in {
                TeamStatus.PAUSED,
                TeamStatus.BLOCKED,
                TeamStatus.WAITING_APPROVAL,
            }
            or status.is_terminal
        )
        if inactive and active_started is not None:
            active_elapsed += max(0.0, (now - active_started).total_seconds())
            active_started = None
        elif not inactive and active_started is None:
            active_started = now
        tasks = (
            tasks_override
            if tasks_override is not None
            else (graph.states() if graph is not None else snapshot.tasks)
        )
        candidate = replace(
            snapshot,
            tasks=tasks,
            status=status,
            stop_reason=stop_reason,
            output=output,
            error=error,
            cycle=snapshot.cycle if cycle is _UNSET else cast(int, cycle),
            plan_revision=(
                snapshot.plan_revision if plan_revision is _UNSET else cast(int, plan_revision)
            ),
            cycle_history=(
                snapshot.cycle_history
                if cycle_history is _UNSET
                else cast(tuple[TeamCycleRecord, ...], cycle_history)
            ),
            pending_interaction=(
                snapshot.pending_interaction
                if pending_interaction is _UNSET
                else cast(HumanInteractionRequest | None, pending_interaction)
            ),
            pending_review=(
                snapshot.pending_review
                if pending_review is _UNSET
                else cast(TeamReview | None, pending_review)
            ),
            human_interactions=(
                snapshot.human_interactions
                if human_interactions is _UNSET
                else cast(tuple[HumanInteractionRecord, ...], human_interactions)
            ),
            review_approved_cycle=(
                snapshot.review_approved_cycle
                if review_approved_cycle is _UNSET
                else cast(int | None, review_approved_cycle)
            ),
            active_elapsed_seconds=active_elapsed,
            active_started_at=active_started,
            updated_at=now,
        )
        return await self._components.repository.save(candidate, snapshot.version)

    async def _publish(
        self,
        event_type: TeamEventType,
        snapshot: TeamSnapshot,
        *,
        detail: str = "",
        metadata: dict[str, object] | None = None,
    ) -> None:
        await self._components.events.publish(
            TeamEvent(
                event_type=event_type,
                snapshot=snapshot,
                detail=detail,
                metadata=metadata or {},
            )
        )

    async def _publish_ready_tasks(
        self,
        snapshot: TeamSnapshot,
        tasks: tuple[TaskSpec, ...],
    ) -> None:
        """发布一组任务进入可调度状态的审计事件。"""
        for task in tasks:
            await self._publish(
                TeamEventType.TASK_READY,
                snapshot,
                metadata={"task_id": task.task_id, "capability": task.capability},
            )

    async def _require(self, run_id: str) -> TeamSnapshot:
        snapshot = await self._components.repository.load(run_id)
        if snapshot is None:
            raise TeamRunNotFoundError(f"team run not found: {run_id}")
        return snapshot

    async def _resolve_conflict(
        self,
        run_id: str,
        error: TeamStateConflictError,
    ) -> TeamResult:
        """外部取消已提交时返回终态，否则保留明确的 CAS 冲突。"""
        latest = await self._components.repository.load(run_id)
        if latest is not None and latest.status.is_terminal:
            return self._result(latest)
        raise error

    def _begin_active(self, run_id: str) -> None:
        if run_id in self._active_runs:
            raise TeamExecutionError(f"team run is already active: {run_id}")
        self._active_runs.add(run_id)

    async def _claim_run(self, run_id: str) -> None:
        """同时取得实例内活动标记与仓储级独占执行租约。"""
        self._begin_active(run_id)
        try:
            acquired = await self._components.repository.acquire_lease(
                run_id,
                self._owner_id,
            )
        except BaseException:
            self._finish_active(run_id)
            raise
        if acquired:
            return
        self._finish_active(run_id)
        raise TeamRunActiveError(f"team run is active: {run_id}")

    def _finish_active(self, run_id: str) -> None:
        self._active_runs.discard(run_id)
        self._cancel_requested.discard(run_id)

    @staticmethod
    def _validate_result(scheduled: _ScheduledTask, result: TaskResult) -> None:
        context = scheduled.context
        if result.task_id != context.task.task_id:
            raise TeamExecutionError("agent returned a result for another task")
        if result.agent_id != context.agent_id:
            raise TeamExecutionError("agent returned a result with another agent identifier")
        if result.attempt != context.attempt:
            raise TeamExecutionError("agent returned a result with another attempt number")

    @staticmethod
    def _same_human_response(left: HumanResponse, right: HumanResponse) -> bool:
        """比较幂等响应的业务内容，忽略提交时间。"""
        return (
            left.interaction_id == right.interaction_id
            and left.action is right.action
            and left.content == right.content
            and dict(left.metadata) == dict(right.metadata)
        )

    @staticmethod
    def _result(snapshot: TeamSnapshot) -> TeamResult:
        results = tuple(
            state.result
            for state in snapshot.tasks
            if state.status is TaskStatus.SUCCEEDED
            and state.result is not None
            and state.result.success
        )
        return TeamResult(
            run_id=snapshot.run_id,
            status=snapshot.status,
            task_results=results,
            output=snapshot.output,
            stop_reason=snapshot.stop_reason,
            error=snapshot.error,
            cycle=snapshot.cycle,
            cycle_history=snapshot.cycle_history,
            pending_interaction=snapshot.pending_interaction,
            human_interactions=snapshot.human_interactions,
            started_at=snapshot.created_at,
            finished_at=snapshot.updated_at if snapshot.status.is_terminal else None,
        )

    @staticmethod
    def _remaining_timeout(snapshot: TeamSnapshot) -> float | None:
        timeout = snapshot.request.limits.timeout_seconds
        if timeout is None:
            return None
        elapsed = snapshot.active_elapsed_seconds
        if snapshot.active_started_at is not None:
            elapsed += max(
                0.0,
                (datetime.now(timezone.utc) - snapshot.active_started_at).total_seconds(),
            )
        return timeout - elapsed

    @staticmethod
    async def _await_with_timeout(
        operation: Coroutine[object, object, TeamResult],
        timeout: float | None,
    ) -> TeamResult:
        """区分控制器总时限与组件主动抛出的 ``TimeoutError``。"""
        if timeout is None:
            return await operation
        task = asyncio.create_task(operation)
        try:
            done, _ = await asyncio.wait({task}, timeout=timeout)
        except asyncio.CancelledError:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            raise
        if task in done:
            return await task
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        raise _TeamDeadlineExceeded


__all__ = ["TeamOrchestrator", "TeamOrchestratorComponents"]
