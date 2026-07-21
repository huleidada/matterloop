"""协调规划、执行、验证、审批、重试与恢复的 Loop 控制器。"""

from __future__ import annotations

import asyncio
import logging
import math
from collections.abc import Awaitable
from datetime import datetime, timezone
from enum import Enum
from threading import RLock
from typing import TypeVar
from uuid import uuid4

from matterloop_core.context import (
    ExecutionResult,
    HumanAction,
    HumanInteractionKind,
    HumanInteractionRecord,
    HumanInteractionRequest,
    HumanResponse,
    IterationRecord,
    LoopContext,
    LoopRequest,
    LoopResult,
    Plan,
    PlanStep,
    result_from_context,
)
from matterloop_core.control import ApprovalDecision, CompletionAction, RetryAction
from matterloop_core.events import LoopEvent, LoopEventType
from matterloop_core.exceptions import (
    CheckpointConflictError,
    HumanInteractionNotPendingError,
    HumanResponseConflictError,
    InvalidPlanError,
    LoopNotFoundError,
    LoopNotResumableError,
    LoopRequestConflictError,
    ResourceLimitExceededError,
)
from matterloop_core.protocols import (
    ApprovalGate,
    CheckpointStore,
    CompletionEvaluator,
    EventPublisher,
    Executor,
    LoopPolicy,
    Planner,
    RetryPolicy,
    Verifier,
)
from matterloop_core.registry import ComponentRegistry
from matterloop_core.state import LoopStatus, ResumeMode, StopReason, ensure_transition

logger = logging.getLogger(__name__)
_T = TypeVar("_T")


class _PlanOutcome(str, Enum):
    """控制器内部用于区分计划完成、重新规划和停止的结果。"""

    COMPLETED = "completed"
    REPLAN = "replan"
    STOPPED = "stopped"


class _LoopStopped(BaseException):
    """表示受监督调用已把 Loop 持久化到稳定停止状态。"""


class AgentLoop:
    """通过有边界的反馈循环协调可替换组件。

    本类只负责编排，不包含模型、工具、数据库或业务策略。规划轮次、执行尝试与单计划
    步骤分别受限；步骤只有显式声明 ``requires_approval`` 时才会进入审批流程。
    """

    def __init__(
        self,
        planners: ComponentRegistry[Planner],
        executors: ComponentRegistry[Executor],
        verifiers: ComponentRegistry[Verifier],
        checkpoint_store: CheckpointStore,
        policy: LoopPolicy,
        events: EventPublisher,
        approval_gate: ApprovalGate,
        retry_policy: RetryPolicy,
        completion_evaluator: CompletionEvaluator | None = None,
        *,
        heartbeat_interval_seconds: float = 5.0,
        cancellation_poll_interval_seconds: float = 0.1,
    ) -> None:
        if not math.isfinite(heartbeat_interval_seconds) or heartbeat_interval_seconds <= 0:
            raise ValueError("heartbeat_interval_seconds must be finite and greater than 0")
        if (
            not math.isfinite(cancellation_poll_interval_seconds)
            or cancellation_poll_interval_seconds <= 0
        ):
            raise ValueError("cancellation_poll_interval_seconds must be finite and greater than 0")
        self.planners = planners
        self.executors = executors
        self.verifiers = verifiers
        self.checkpoint_store = checkpoint_store
        self.policy = policy
        self.events = events
        self.approval_gate = approval_gate
        self.retry_policy = retry_policy
        self.completion_evaluator = completion_evaluator
        self.heartbeat_interval_seconds = heartbeat_interval_seconds
        self.cancellation_poll_interval_seconds = cancellation_poll_interval_seconds
        self._cancelled_runs: set[str] = set()
        self._cancellation_lock = RLock()

    @staticmethod
    def create_run_id() -> str:
        """预先生成运行标识，便于调用方在并发执行期间发起取消。"""
        return uuid4().hex

    def cancel(self, run_id: str) -> bool:
        """请求在下一个安全边界取消指定运行。

        Returns:
            本次调用是否首次登记该运行的取消请求。
        """
        normalized_run_id = run_id.strip()
        if not normalized_run_id:
            raise ValueError("run_id must not be empty")
        with self._cancellation_lock:
            was_added = normalized_run_id not in self._cancelled_runs
            self._cancelled_runs.add(normalized_run_id)
            return was_added

    async def run(
        self,
        request: LoopRequest,
        *,
        planner: str = "default",
        verifier: str = "default",
        run_id: str | None = None,
    ) -> LoopResult:
        """启动新的 Loop，直到完成或触发停止边界。

        每个步骤使用自身的 ``PlanStep.executor`` 选择执行器，因此调用方无需在运行入口
        提供一个会覆盖整个计划的执行器名称。
        """
        actual_run_id = run_id or self.create_run_id()
        if not actual_run_id.strip():
            raise ValueError("run_id must not be empty")
        if run_id is not None:
            existing = await self.checkpoint_store.load(actual_run_id)
            if existing is not None:
                self._ensure_same_request(existing.request, request, actual_run_id)
                return result_from_context(existing)

        context = LoopContext(request=request, run_id=actual_run_id)
        self._start_active_timer(context)
        context.last_heartbeat_at = datetime.now(timezone.utc)
        try:
            await self._checkpoint_and_emit(context, LoopEventType.LOOP_STARTED)
        except CheckpointConflictError:
            existing = await self.checkpoint_store.load(actual_run_id)
            if existing is None:
                raise
            self._ensure_same_request(existing.request, request, actual_run_id)
            return result_from_context(existing)
        return await self._drive(
            context,
            planner_name=planner,
            verifier_name=verifier,
            continue_current_plan=False,
        )

    async def resume(
        self,
        run_id: str,
        *,
        mode: ResumeMode = ResumeMode.CONTINUE,
        planner: str = "default",
        verifier: str = "default",
    ) -> LoopResult:
        """从暂停或阻塞检查点恢复运行。

        ``CONTINUE`` 会从 ``current_step_index`` 精确继续原计划；``REPLAN`` 会丢弃原计划
        并让规划器开始新一轮。前者缺少可继续计划时会明确失败，不会隐式重新规划。

        Raises:
            LoopNotFoundError: 当指定检查点不存在时抛出。
            LoopNotResumableError: 当状态或恢复模式不允许继续时抛出。
        """
        context = await self.checkpoint_store.load(run_id)
        if context is None:
            raise LoopNotFoundError(run_id)
        if context.status not in {LoopStatus.PAUSED, LoopStatus.BLOCKED}:
            raise LoopNotResumableError(context.status.value)
        if context.pending_interaction is not None:
            raise LoopNotResumableError("checkpoint is waiting for a human response")
        if context.stop_reason is StopReason.HUMAN_REJECTED and mode is ResumeMode.CONTINUE:
            raise LoopNotResumableError("human-rejected runs require explicit replan")
        if context.stop_reason is StopReason.RECOVERY_REQUIRED:
            raise LoopNotResumableError(
                "recovery-required runs must be reconciled by the host before resuming"
            )

        effective_mode = ResumeMode.REPLAN if context.replan_required else mode
        continue_current_plan = effective_mode is ResumeMode.CONTINUE
        if continue_current_plan and (
            context.current_plan is None
            or (
                context.current_step_index >= len(context.current_plan.steps)
                and not context.completion_approved
            )
        ):
            raise LoopNotResumableError("checkpoint has no unfinished plan")

        context.stop_reason = None
        context.error = ""
        self._start_active_timer(context)
        if effective_mode is ResumeMode.REPLAN:
            context.current_plan = None
            context.current_step_index = 0
            context.approved_step_ids.clear()
            context.completion_approved = False
            context.replan_required = False
            await self._transition(context, LoopStatus.PLANNING, LoopEventType.LOOP_RESUMED)
            await self._checkpoint_and_emit(context, LoopEventType.PLANNING_STARTED)
        else:
            await self._checkpoint_and_emit(context, LoopEventType.LOOP_RESUMED)

        return await self._drive(
            context,
            planner_name=planner,
            verifier_name=verifier,
            continue_current_plan=continue_current_plan,
        )

    async def recover(
        self,
        run_id: str,
        *,
        planner: str = "default",
        verifier: str = "default",
    ) -> LoopResult:
        """恢复进程中断遗留的非稳定检查点，且不盲目重放外部计算。

        已持久化 ``pending_execution`` 的运行会直接进入验证；停在执行中但没有结果的
        运行无法判断外部副作用是否完成，因此会进入 ``RECOVERY_REQUIRED`` 阻塞状态，
        等待宿主按 ``active_operation_id`` 对账，不会自动再次调用执行器。

        Args:
            run_id: 需要恢复的运行标识。
            planner: 规划器注册名。
            verifier: 验证器注册名。

        Returns:
            恢复后的运行结果或稳定阻塞快照。
        """
        context = await self.checkpoint_store.load(run_id)
        if context is None:
            raise LoopNotFoundError(run_id)
        if context.status.is_terminal or context.status in {LoopStatus.PAUSED, LoopStatus.BLOCKED}:
            return result_from_context(context)

        self._settle_crashed_timer(context)
        self._start_active_timer(context)
        if context.status is LoopStatus.EXECUTING and context.pending_execution is None:
            context.error = (
                "执行状态无法确认；请使用 active_operation_id 对账，MatterLoop 未重复提交"
            )
            await self._stop(
                context,
                LoopStatus.BLOCKED,
                StopReason.RECOVERY_REQUIRED,
                LoopEventType.LOOP_BLOCKED,
            )
            return result_from_context(context)
        if context.status is LoopStatus.WAITING_APPROVAL:
            context.error = "审批调用状态无法确认；MatterLoop 未重复请求审批"
            await self._stop(
                context,
                LoopStatus.BLOCKED,
                StopReason.RECOVERY_REQUIRED,
                LoopEventType.LOOP_BLOCKED,
            )
            return result_from_context(context)

        continue_current_plan = context.current_plan is not None and context.status in {
            LoopStatus.EXECUTING,
            LoopStatus.VERIFYING,
        }
        await self._checkpoint_and_emit(context, LoopEventType.LOOP_RECOVERED)
        return await self._drive(
            context,
            planner_name=planner,
            verifier_name=verifier,
            continue_current_plan=continue_current_plan,
        )

    async def submit_human_response(
        self,
        run_id: str,
        response: HumanResponse,
    ) -> LoopResult:
        """幂等提交人工反馈，但不自动恢复 Loop 执行。

        相同幂等键与相同内容重复提交时返回当前结果；同一幂等键携带不同内容时抛出
        ``HumanResponseConflictError``。批准会保留精确步骤游标，修改意见和补充输入会
        标记下一次 ``resume`` 必须重新规划。

        Args:
            run_id: 待处理运行标识。
            response: 与当前待处理交互匹配的响应。

        Returns:
            提交后的不可变运行快照。
        """
        context = await self.checkpoint_store.load(run_id)
        if context is None:
            raise LoopNotFoundError(run_id)

        existing = self._find_response_by_idempotency_key(context, response.idempotency_key)
        if existing is not None:
            if self._same_response(existing.response, response):
                return result_from_context(context)
            raise HumanResponseConflictError(response.idempotency_key)

        pending = context.pending_interaction
        if pending is None:
            raise HumanInteractionNotPendingError("run has no pending human interaction")
        if pending.interaction_id != response.interaction_id:
            raise HumanInteractionNotPendingError(response.interaction_id)
        if response.action not in pending.allowed_actions:
            raise HumanInteractionNotPendingError(
                f"action {response.action.value} is not allowed for this interaction"
            )

        context.human_interactions.append(HumanInteractionRecord(pending, response))
        context.pending_interaction = None
        if response.content:
            context.feedback = response.content

        if response.action is HumanAction.APPROVE:
            if pending.kind is HumanInteractionKind.APPROVAL and pending.step_id is not None:
                context.approved_step_ids.add(pending.step_id)
            if pending.kind is HumanInteractionKind.COMPLETION_REVIEW or (
                context.current_plan is not None
                and context.current_step_index >= len(context.current_plan.steps)
            ):
                context.completion_approved = True
        elif response.action in {HumanAction.REVISE, HumanAction.PROVIDE_INPUT}:
            context.replan_required = True

        event_types: tuple[LoopEventType, ...]
        if response.action is HumanAction.REJECT:
            self._pause_active_timer(context)
            context.stop_reason = StopReason.HUMAN_REJECTED
            if context.status is not LoopStatus.BLOCKED:
                ensure_transition(context.status, LoopStatus.BLOCKED)
                context.status = LoopStatus.BLOCKED
            event_types = (
                LoopEventType.HUMAN_RESPONSE_SUBMITTED,
                LoopEventType.HUMAN_REJECTED,
                LoopEventType.LOOP_BLOCKED,
            )
        elif response.action is HumanAction.APPROVE:
            event_types = (
                LoopEventType.HUMAN_RESPONSE_SUBMITTED,
                LoopEventType.HUMAN_APPROVED,
            )
        elif response.action is HumanAction.REVISE:
            event_types = (
                LoopEventType.HUMAN_RESPONSE_SUBMITTED,
                LoopEventType.HUMAN_REVISED,
            )
        else:
            event_types = (
                LoopEventType.HUMAN_RESPONSE_SUBMITTED,
                LoopEventType.HUMAN_INPUT_PROVIDED,
            )
        try:
            await self._checkpoint_and_emit_many(context, event_types)
        except CheckpointConflictError:
            latest = await self.checkpoint_store.load(run_id)
            if latest is not None:
                committed = self._find_response_by_idempotency_key(latest, response.idempotency_key)
                if committed is not None and self._same_response(committed.response, response):
                    return result_from_context(latest)
            raise
        return result_from_context(context)

    async def _drive(
        self,
        context: LoopContext,
        *,
        planner_name: str,
        verifier_name: str,
        continue_current_plan: bool,
    ) -> LoopResult:
        try:
            await self._run_cycles(
                context,
                planner_name=planner_name,
                verifier_name=verifier_name,
                continue_current_plan=continue_current_plan,
            )
            if context.status not in {
                LoopStatus.PAUSED,
                LoopStatus.BLOCKED,
                LoopStatus.COMPLETED,
                LoopStatus.CANCELLED,
                LoopStatus.TIMED_OUT,
                LoopStatus.FAILED,
            }:
                await self._fail(
                    context,
                    RuntimeError("Loop driver exited without a stable terminal state"),
                )
        except _LoopStopped:
            pass
        except asyncio.CancelledError:
            if not context.status.is_terminal:
                terminal_write = asyncio.create_task(
                    self._stop(
                        context,
                        LoopStatus.CANCELLED,
                        StopReason.CANCELLED,
                        LoopEventType.LOOP_CANCELLED,
                    )
                )
                try:
                    await asyncio.shield(terminal_write)
                except asyncio.CancelledError:
                    await terminal_write
            raise
        except CheckpointConflictError:
            # 另一个控制器已经推进同一运行；不得用陈旧上下文覆盖胜者状态。
            raise
        except ResourceLimitExceededError as exc:
            context.error = f"{type(exc).__name__}: {exc}"
            await self._stop(
                context,
                LoopStatus.BLOCKED,
                StopReason.BUDGET_EXHAUSTED,
                LoopEventType.LOOP_BLOCKED,
            )
        except Exception as exc:
            await self._fail(context, exc)
            raise
        finally:
            with self._cancellation_lock:
                self._cancelled_runs.discard(context.run_id)
        return result_from_context(context)

    async def _run_cycles(
        self,
        context: LoopContext,
        *,
        planner_name: str,
        verifier_name: str,
        continue_current_plan: bool,
    ) -> None:
        if continue_current_plan:
            plan = context.current_plan
            if plan is None:
                raise LoopNotResumableError("checkpoint has no current plan")
            outcome = await self._execute_plan(context, plan, verifier_name)
            if await self._finish_plan_outcome(context, outcome):
                return

        while True:
            if await self._stop_at_safe_boundary(context):
                return
            if context.cycle_count >= context.request.limits.max_cycles:
                await self._stop(
                    context,
                    LoopStatus.BLOCKED,
                    StopReason.CYCLE_LIMIT,
                    LoopEventType.LOOP_BLOCKED,
                )
                return
            if context.status is not LoopStatus.PLANNING:
                await self._transition(context, LoopStatus.PLANNING, LoopEventType.PLANNING_STARTED)

            context.cycle_count += 1
            plan = await self._await_component(
                context,
                self.planners.get(planner_name).plan(context.snapshot()),
                component=f"planner:{planner_name}",
            )
            if not plan.steps:
                raise InvalidPlanError("planner returned an empty plan")
            if len(plan.steps) > context.request.limits.max_steps_per_plan:
                context.current_plan = None
                context.current_step_index = 0
                await self._stop(
                    context,
                    LoopStatus.BLOCKED,
                    StopReason.STEP_LIMIT,
                    LoopEventType.LOOP_BLOCKED,
                )
                return
            self._validate_plan(plan)
            context.current_plan = plan
            context.current_step_index = 0
            await self._checkpoint_and_emit(context, LoopEventType.PLAN_CREATED)

            outcome = await self._execute_plan(context, plan, verifier_name)
            if await self._finish_plan_outcome(context, outcome):
                return

    async def _execute_plan(
        self,
        context: LoopContext,
        plan: Plan,
        verifier_name: str,
    ) -> _PlanOutcome:
        for step_index in range(context.current_step_index, len(plan.steps)):
            step = plan.steps[step_index]
            context.current_step_index = step_index
            if await self._stop_at_safe_boundary(context):
                return _PlanOutcome.STOPPED

            if context.pending_execution is None:
                if step.requires_approval:
                    if not await self._approve(context, step):
                        return _PlanOutcome.STOPPED
                elif context.status is not LoopStatus.EXECUTING:
                    await self._transition(
                        context, LoopStatus.EXECUTING, LoopEventType.EXECUTION_STARTED
                    )

                execution_with_attempt = await self._execute(context, step)
                if execution_with_attempt is None:
                    if context.status is LoopStatus.PLANNING:
                        return _PlanOutcome.REPLAN
                    return _PlanOutcome.STOPPED
                execution, attempt = execution_with_attempt
            else:
                if context.active_operation_id != self._operation_id(context, step):
                    raise LoopNotResumableError(
                        "pending execution does not belong to the current plan step"
                    )
                execution = context.pending_execution
                pending_attempt = context.pending_attempt
                if pending_attempt is None:  # pragma: no cover - codec enforces this invariant
                    raise LoopNotResumableError("pending execution has no attempt number")
                attempt = pending_attempt

            if context.status is not LoopStatus.VERIFYING:
                await self._transition(
                    context, LoopStatus.VERIFYING, LoopEventType.VERIFICATION_STARTED
                )
            verification = await self._await_component(
                context,
                self.verifiers.get(verifier_name).verify(
                    step,
                    execution,
                    context.snapshot(),
                ),
                component=f"verifier:{verifier_name}",
            )
            context.records.append(
                IterationRecord(
                    cycle=context.cycle_count,
                    step_index=step_index,
                    step=step,
                    execution=execution,
                    verification=verification,
                    attempt=attempt,
                )
            )
            context.completed_steps += 1
            context.current_step_index = step_index + 1
            context.feedback = verification.feedback
            context.approved_step_ids.discard(step.step_id)
            context.active_operation_id = None
            context.pending_execution = None
            context.pending_attempt = None
            await self._checkpoint_and_emit(context, LoopEventType.ITERATION_COMPLETED)

            if not verification.passed:
                context.current_plan = None
                context.current_step_index = 0
                await self._transition(context, LoopStatus.PLANNING, LoopEventType.PLANNING_STARTED)
                return _PlanOutcome.REPLAN
        return _PlanOutcome.COMPLETED

    async def _finish_plan_outcome(self, context: LoopContext, outcome: _PlanOutcome) -> bool:
        """处理计划结果，并返回当前驱动器是否应当结束。"""
        if outcome is _PlanOutcome.STOPPED:
            return True
        if outcome is _PlanOutcome.REPLAN:
            return False

        if context.completion_approved:
            context.completion_approved = False
            await self._stop(
                context,
                LoopStatus.COMPLETED,
                StopReason.COMPLETED,
                LoopEventType.LOOP_COMPLETED,
            )
            return True

        evaluator = self.completion_evaluator
        if evaluator is not None:
            await self._checkpoint_and_emit(context, LoopEventType.COMPLETION_EVALUATION_STARTED)
            decision = await self._await_component(
                context,
                evaluator.evaluate(context.snapshot()),
                component="completion_evaluator",
            )
            if decision.feedback:
                context.feedback = decision.feedback
            if decision.action is CompletionAction.REPLAN:
                context.current_plan = None
                context.current_step_index = 0
                await self._checkpoint_and_emit(context, LoopEventType.COMPLETION_REPLAN_REQUESTED)
                await self._transition(context, LoopStatus.PLANNING, LoopEventType.PLANNING_STARTED)
                return False
            if decision.action is CompletionAction.REQUEST_HUMAN:
                interaction = decision.interaction
                if interaction is None:  # pragma: no cover - 值对象已保证该不变量
                    raise RuntimeError("completion evaluator omitted human interaction")
                await self._request_human_interaction(context, interaction)
                return True
            if decision.action is CompletionAction.STOP:
                await self._stop(
                    context,
                    LoopStatus.BLOCKED,
                    StopReason.COMPLETION_REJECTED,
                    LoopEventType.LOOP_BLOCKED,
                )
                return True

        await self._stop(
            context,
            LoopStatus.COMPLETED,
            StopReason.COMPLETED,
            LoopEventType.LOOP_COMPLETED,
        )
        return True

    async def _approve(self, context: LoopContext, step: PlanStep) -> bool:
        if step.step_id in context.approved_step_ids:
            await self._checkpoint_and_emit(context, LoopEventType.APPROVAL_GRANTED)
            await self._transition(context, LoopStatus.EXECUTING, LoopEventType.EXECUTION_STARTED)
            return True

        await self._transition(
            context, LoopStatus.WAITING_APPROVAL, LoopEventType.APPROVAL_REQUESTED
        )
        decision = await self._await_component(
            context,
            self.approval_gate.decide(step, context.snapshot()),
            component="approval_gate",
        )
        if decision is ApprovalDecision.APPROVED:
            await self._checkpoint_and_emit(context, LoopEventType.APPROVAL_GRANTED)
            await self._transition(context, LoopStatus.EXECUTING, LoopEventType.EXECUTION_STARTED)
            return True
        if decision is ApprovalDecision.DEFERRED:
            await self._request_human_interaction(
                context,
                HumanInteractionRequest(
                    kind=HumanInteractionKind.APPROVAL,
                    prompt=f"是否批准执行步骤：{step.description}",
                    allowed_actions=(
                        HumanAction.APPROVE,
                        HumanAction.REJECT,
                        HumanAction.REVISE,
                    ),
                    step_id=step.step_id,
                    metadata={"executor": step.executor},
                ),
                reason=StopReason.APPROVAL_DEFERRED,
            )
            return False
        await self._stop(
            context,
            LoopStatus.BLOCKED,
            StopReason.APPROVAL_REJECTED,
            LoopEventType.LOOP_BLOCKED,
        )
        return False

    async def _execute(
        self, context: LoopContext, step: PlanStep
    ) -> tuple[ExecutionResult, int] | None:
        attempt = 1
        operation_id = self._operation_id(context, step)
        if context.active_operation_id not in {None, operation_id}:
            raise LoopNotResumableError("another execution operation is already active")
        context.active_operation_id = operation_id
        while True:
            if await self._stop_at_safe_boundary(context):
                return None
            if context.total_attempts >= context.request.limits.max_attempts:
                await self._stop(
                    context,
                    LoopStatus.BLOCKED,
                    StopReason.ATTEMPT_LIMIT,
                    LoopEventType.LOOP_BLOCKED,
                )
                return None

            context.total_attempts += 1
            context.pending_attempt = attempt
            await self._checkpoint_and_emit(
                context,
                LoopEventType.EXECUTION_DISPATCHED,
                detail=operation_id,
            )
            try:
                result = await self._await_component(
                    context,
                    self.executors.get(step.executor).execute(step, context.snapshot()),
                    component=f"executor:{step.executor}",
                )
                context.pending_execution = result
                await self._checkpoint_and_emit(
                    context,
                    LoopEventType.EXECUTION_COMPLETED,
                    detail=operation_id,
                )
                return result, attempt
            except ResourceLimitExceededError as exc:
                context.active_operation_id = None
                context.pending_attempt = None
                context.error = f"{type(exc).__name__}: {exc}"
                await self._stop(
                    context,
                    LoopStatus.BLOCKED,
                    StopReason.BUDGET_EXHAUSTED,
                    LoopEventType.LOOP_BLOCKED,
                )
                return None
            except Exception as exc:
                decision = self.retry_policy.decide(exc, attempt, context.snapshot())
                if decision.action is RetryAction.FAIL:
                    raise
                if decision.action is RetryAction.REPLAN:
                    context.feedback = f"{type(exc).__name__}: {exc}"
                    context.current_plan = None
                    context.current_step_index = 0
                    context.active_operation_id = None
                    context.pending_attempt = None
                    await self._transition(
                        context, LoopStatus.PLANNING, LoopEventType.COMPONENT_RETRYING
                    )
                    return None
                attempt += 1
                await self._checkpoint_and_emit(
                    context, LoopEventType.COMPONENT_RETRYING, detail=str(attempt)
                )
                if decision.delay_seconds:
                    await self._await_component(
                        context,
                        asyncio.sleep(decision.delay_seconds),
                        component="retry_backoff",
                    )

    async def _stop_at_safe_boundary(self, context: LoopContext) -> bool:
        if self._is_cancelled(context.run_id):
            await self._stop(
                context,
                LoopStatus.CANCELLED,
                StopReason.CANCELLED,
                LoopEventType.LOOP_CANCELLED,
            )
            return True
        if not self.policy.can_continue(context.snapshot()):
            await self._stop(
                context,
                LoopStatus.BLOCKED,
                StopReason.POLICY_REJECTED,
                LoopEventType.LOOP_BLOCKED,
            )
            return True
        return False

    def _is_cancelled(self, run_id: str) -> bool:
        """在线程锁保护下读取协作式取消标记。"""
        with self._cancellation_lock:
            return run_id in self._cancelled_runs

    async def _await_component(
        self,
        context: LoopContext,
        awaitable: Awaitable[_T],
        *,
        component: str,
    ) -> _T:
        """监督长调用，周期写入心跳并及时执行取消和总超时。"""
        task = asyncio.ensure_future(awaitable)
        loop = asyncio.get_running_loop()
        next_heartbeat = loop.time() + self.heartbeat_interval_seconds
        try:
            while True:
                if self._is_cancelled(context.run_id):
                    await self._cancel_component_task(task)
                    await self._stop(
                        context,
                        LoopStatus.CANCELLED,
                        StopReason.CANCELLED,
                        LoopEventType.LOOP_CANCELLED,
                    )
                    raise _LoopStopped

                remaining = self._remaining_timeout(context)
                if remaining is not None and remaining <= 0:
                    await self._cancel_component_task(task)
                    await self._stop(
                        context,
                        LoopStatus.TIMED_OUT,
                        StopReason.TIMED_OUT,
                        LoopEventType.LOOP_TIMED_OUT,
                    )
                    raise _LoopStopped

                now = loop.time()
                wait_seconds = min(
                    self.cancellation_poll_interval_seconds,
                    max(0.0, next_heartbeat - now),
                )
                if remaining is not None:
                    wait_seconds = min(wait_seconds, remaining)
                done, _ = await asyncio.wait(
                    (task,),
                    timeout=wait_seconds,
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if task in done:
                    if self._is_cancelled(context.run_id):
                        await self._stop(
                            context,
                            LoopStatus.CANCELLED,
                            StopReason.CANCELLED,
                            LoopEventType.LOOP_CANCELLED,
                        )
                        raise _LoopStopped
                    return task.result()

                now = loop.time()
                if now >= next_heartbeat:
                    context.last_heartbeat_at = datetime.now(timezone.utc)
                    await self._checkpoint_and_emit(
                        context,
                        LoopEventType.LOOP_HEARTBEAT,
                        detail=component,
                    )
                    next_heartbeat = now + self.heartbeat_interval_seconds
        finally:
            if not task.done():
                await self._cancel_component_task(task)

    @staticmethod
    async def _cancel_component_task(task: asyncio.Future[_T]) -> None:
        """取消并回收组件任务，避免后台协程在 Loop 停止后继续产生副作用。"""
        if task.done():
            return
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    @staticmethod
    def _operation_id(context: LoopContext, step: PlanStep) -> str:
        """为同一逻辑步骤生成跨重试和进程恢复稳定的执行幂等键。"""
        return f"{context.run_id}:{context.cycle_count}:{step.step_id}"

    @staticmethod
    def _ensure_same_request(
        existing: LoopRequest,
        incoming: LoopRequest,
        run_id: str,
    ) -> None:
        """防止调用方把同一运行标识复用于另一业务请求。"""
        if existing != incoming:
            raise LoopRequestConflictError(
                f"run_id already belongs to a different request: {run_id}"
            )

    @staticmethod
    def _validate_plan(plan: Plan) -> None:
        """保证计划内步骤标识唯一，避免恢复时选择错误步骤。"""
        step_ids = [step.step_id for step in plan.steps]
        if len(step_ids) != len(set(step_ids)):
            raise InvalidPlanError("planner returned duplicate step identifiers")

    @staticmethod
    def _remaining_timeout(context: LoopContext) -> float | None:
        timeout = context.request.limits.timeout_seconds
        if timeout is None:
            return None
        elapsed = context.active_elapsed_seconds
        if context.active_started_at is not None:
            elapsed += (datetime.now(timezone.utc) - context.active_started_at).total_seconds()
        return timeout - elapsed

    @staticmethod
    def _start_active_timer(context: LoopContext) -> None:
        """开始计算活跃执行时间，不重复启动已有计时段。"""
        if context.active_started_at is None:
            context.active_started_at = datetime.now(timezone.utc)

    @staticmethod
    def _pause_active_timer(context: LoopContext) -> None:
        """结算当前活跃计时段，使人工等待不占用运行超时。"""
        if context.active_started_at is None:
            return
        elapsed = (datetime.now(timezone.utc) - context.active_started_at).total_seconds()
        context.active_elapsed_seconds += max(0, elapsed)
        context.active_started_at = None

    @staticmethod
    def _settle_crashed_timer(context: LoopContext) -> None:
        """按最后心跳结算崩溃前活跃时间，不把进程离线时长计入超时。"""
        if context.active_started_at is None:
            return
        observed_until = context.last_heartbeat_at or context.updated_at
        elapsed = (observed_until - context.active_started_at).total_seconds()
        context.active_elapsed_seconds += max(0, elapsed)
        context.active_started_at = None

    async def _request_human_interaction(
        self,
        context: LoopContext,
        interaction: HumanInteractionRequest,
        *,
        reason: StopReason = StopReason.HUMAN_INPUT_REQUIRED,
    ) -> None:
        """持久化人工请求并把 Loop 置为不计活跃超时的暂停状态。"""
        if context.pending_interaction is not None:
            raise HumanInteractionNotPendingError("run already has a pending human interaction")
        context.pending_interaction = interaction
        context.stop_reason = reason
        self._pause_active_timer(context)
        if context.status is LoopStatus.PAUSED:
            await self._checkpoint_and_emit(context, LoopEventType.HUMAN_INTERACTION_REQUESTED)
        else:
            await self._transition(
                context,
                LoopStatus.PAUSED,
                LoopEventType.HUMAN_INTERACTION_REQUESTED,
            )
        await self._checkpoint_and_emit(context, LoopEventType.LOOP_PAUSED)

    @staticmethod
    def _find_response_by_idempotency_key(
        context: LoopContext,
        idempotency_key: str,
    ) -> HumanInteractionRecord | None:
        """按调用方幂等键查找已经提交的人工响应。"""
        return next(
            (
                record
                for record in context.human_interactions
                if record.response.idempotency_key == idempotency_key
            ),
            None,
        )

    @staticmethod
    def _same_response(existing: HumanResponse, incoming: HumanResponse) -> bool:
        """比较影响 Loop 行为的响应字段，忽略客户端重试产生的时间差。"""
        return (
            existing.interaction_id == incoming.interaction_id
            and existing.action is incoming.action
            and existing.content == incoming.content
            and dict(existing.metadata) == dict(incoming.metadata)
        )

    async def _transition(
        self, context: LoopContext, target: LoopStatus, event_type: LoopEventType
    ) -> None:
        ensure_transition(context.status, target)
        context.status = target
        context.updated_at = datetime.now(timezone.utc)
        await self._checkpoint_and_emit(context, event_type)

    async def _stop(
        self,
        context: LoopContext,
        status: LoopStatus,
        reason: StopReason,
        event_type: LoopEventType,
    ) -> None:
        self._pause_active_timer(context)
        context.stop_reason = reason
        if context.status is status:
            await self._checkpoint_and_emit(context, event_type)
        else:
            await self._transition(context, status, event_type)

    async def _checkpoint_and_emit(
        self, context: LoopContext, event_type: LoopEventType, detail: str = ""
    ) -> None:
        await self._checkpoint_and_emit_many(context, (event_type,), detail=detail)

    async def _checkpoint_and_emit_many(
        self,
        context: LoopContext,
        event_types: tuple[LoopEventType, ...],
        *,
        detail: str = "",
    ) -> None:
        """原子提交一次状态变化，并按顺序发布与其对应的多个审计事件。"""
        if not event_types:
            return
        context.updated_at = datetime.now(timezone.utc)
        first_sequence = context.event_sequence + 1
        context.event_sequence += len(event_types)
        expected_revision = context.revision
        revision = await self.checkpoint_store.save(
            context.snapshot(), expected_revision=expected_revision
        )
        if revision <= expected_revision:
            raise CheckpointConflictError(
                f"checkpoint revision did not advance: {expected_revision} -> {revision}"
            )
        context.revision = revision
        snapshot = context.snapshot()
        for offset, event_type in enumerate(event_types):
            await self.events.publish(
                LoopEvent(
                    event_type,
                    snapshot,
                    detail=detail,
                    sequence=first_sequence + offset,
                )
            )

    async def _fail(self, context: LoopContext, error: Exception) -> None:
        self._pause_active_timer(context)
        context.stop_reason = StopReason.COMPONENT_ERROR
        context.error = f"{type(error).__name__}: {error}"
        if not context.status.is_terminal:
            await self._transition(context, LoopStatus.FAILED, LoopEventType.LOOP_FAILED)
        logger.exception("Loop 执行失败", extra={"run_id": context.run_id})
