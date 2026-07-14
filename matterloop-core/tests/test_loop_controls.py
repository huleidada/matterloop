"""Loop 预算、审批、重试、取消、超时与恢复能力测试。"""

import asyncio

import pytest
from conftest import build_loop
from matterloop_core import (
    ApprovalDecision,
    ExecutionResult,
    HumanAction,
    HumanResponse,
    LoopContext,
    LoopLimits,
    LoopNotResumableError,
    LoopRequest,
    LoopStatus,
    Plan,
    PlanStep,
    ResourceLimitExceededError,
    ResumeMode,
    RetryAction,
    RetryDecision,
    StopReason,
    VerificationResult,
)


class TwoStepPlanner:
    """生成两个步骤，用于验证完整计划执行。"""

    async def plan(self, context: LoopContext) -> Plan:
        """返回固定的两步骤计划。"""
        del context
        return Plan((PlanStep("first"), PlanStep("second")))


def test_loop_executes_every_step_in_plan() -> None:
    """全部步骤验证通过后才允许 Loop 完成。"""
    loop, _, _ = build_loop()
    loop.planners.register("default", TwoStepPlanner(), replace=True)

    result = asyncio.run(loop.run(LoopRequest(goal="multi")))

    assert result.status is LoopStatus.COMPLETED
    assert result.stop_reason is StopReason.COMPLETED
    assert result.cycles == 1
    assert [record.execution.output for record in result.records] == ["first", "second"]


def test_plan_step_budget_is_independent_from_attempt_budget() -> None:
    """超长计划应命中步骤预算，且不能消耗任何执行尝试。"""
    loop, _, _ = build_loop()
    loop.planners.register("default", TwoStepPlanner(), replace=True)
    request = LoopRequest(
        goal="limited",
        limits=LoopLimits(max_cycles=3, max_attempts=10, max_steps_per_plan=1),
    )

    result = asyncio.run(loop.run(request))

    assert result.status is LoopStatus.BLOCKED
    assert result.stop_reason is StopReason.STEP_LIMIT
    assert result.cycles == 1
    assert result.total_attempts == 0


def test_only_marked_step_requests_approval() -> None:
    """没有风险标记的步骤不得触发审批门。"""
    loop, _, _ = build_loop()

    class CountingApproval:
        def __init__(self) -> None:
            self.calls = 0

        async def decide(self, step: PlanStep, context: LoopContext) -> ApprovalDecision:
            del step, context
            self.calls += 1
            return ApprovalDecision.APPROVED

    gate = CountingApproval()
    loop.approval_gate = gate
    result = asyncio.run(loop.run(LoopRequest(goal="safe")))

    assert result.status is LoopStatus.COMPLETED
    assert gate.calls == 0


def test_deferred_approval_continues_exact_step_without_replanning() -> None:
    """默认恢复应继续暂停步骤，而不是再次调用规划器。"""
    loop, _, _ = build_loop()

    class ApprovalPlanner:
        def __init__(self) -> None:
            self.calls = 0

        async def plan(self, context: LoopContext) -> Plan:
            del context
            self.calls += 1
            return Plan((PlanStep("approved task", requires_approval=True),))

    class DeferredOnceApproval:
        def __init__(self) -> None:
            self.calls = 0

        async def decide(self, step: PlanStep, context: LoopContext) -> ApprovalDecision:
            del step, context
            self.calls += 1
            return ApprovalDecision.DEFERRED if self.calls == 1 else ApprovalDecision.APPROVED

    planner = ApprovalPlanner()
    loop.planners.register("default", planner, replace=True)
    loop.approval_gate = DeferredOnceApproval()
    paused = asyncio.run(loop.run(LoopRequest(goal="approval")))
    assert paused.pending_interaction is not None
    asyncio.run(
        loop.submit_human_response(
            paused.run_id,
            HumanResponse(paused.pending_interaction.interaction_id, HumanAction.APPROVE),
        )
    )
    resumed = asyncio.run(loop.resume(paused.run_id))

    assert paused.status is LoopStatus.PAUSED
    assert paused.stop_reason is StopReason.APPROVAL_DEFERRED
    assert resumed.status is LoopStatus.COMPLETED
    assert resumed.cycles == 1
    assert planner.calls == 1


def test_replan_resume_discards_paused_plan() -> None:
    """显式重新规划恢复应产生新计划轮次。"""
    loop, _, _ = build_loop()

    class VersionedPlanner:
        def __init__(self) -> None:
            self.calls = 0

        async def plan(self, context: LoopContext) -> Plan:
            del context
            self.calls += 1
            return Plan((PlanStep(f"plan-{self.calls}", requires_approval=self.calls == 1),))

    class DeferredApproval:
        async def decide(self, step: PlanStep, context: LoopContext) -> ApprovalDecision:
            del step, context
            return ApprovalDecision.DEFERRED

    planner = VersionedPlanner()
    loop.planners.register("default", planner, replace=True)
    loop.approval_gate = DeferredApproval()
    paused = asyncio.run(loop.run(LoopRequest(goal="replan")))
    assert paused.pending_interaction is not None
    asyncio.run(
        loop.submit_human_response(
            paused.run_id,
            HumanResponse(
                paused.pending_interaction.interaction_id,
                HumanAction.REVISE,
                "请采用第二版方案",
            ),
        )
    )
    resumed = asyncio.run(loop.resume(paused.run_id, mode=ResumeMode.REPLAN))

    assert resumed.status is LoopStatus.COMPLETED
    assert resumed.output == "plan-2"
    assert resumed.cycles == 2
    assert planner.calls == 2


def test_continue_requires_unfinished_plan() -> None:
    """继续模式缺少原计划时不得静默退化为重新规划。"""
    loop, store, _ = build_loop()
    blocked = LoopContext(LoopRequest(goal="missing"), status=LoopStatus.BLOCKED)
    asyncio.run(store.save(blocked))

    with pytest.raises(LoopNotResumableError):
        asyncio.run(loop.resume(blocked.run_id))


def test_executor_error_can_retry_and_recover() -> None:
    """重试策略允许时，临时执行异常应在同一步骤恢复。"""
    loop, _, _ = build_loop()

    class FailOnceExecutor:
        def __init__(self) -> None:
            self.calls = 0

        async def execute(self, step: PlanStep, context: LoopContext) -> ExecutionResult:
            del context
            self.calls += 1
            if self.calls == 1:
                raise RuntimeError("temporary")
            return ExecutionResult(step.description)

    class RetryOnce:
        def decide(self, error: Exception, attempt: int, context: LoopContext) -> RetryDecision:
            del error, context
            action = RetryAction.RETRY if attempt == 1 else RetryAction.FAIL
            return RetryDecision(action)

    executor = FailOnceExecutor()
    loop.executors.register("default", executor, replace=True)
    loop.retry_policy = RetryOnce()

    result = asyncio.run(loop.run(LoopRequest(goal="retry")))

    assert result.status is LoopStatus.COMPLETED
    assert executor.calls == 2
    assert result.total_attempts == 2
    assert result.cycles == 1
    assert result.records[0].attempt == 2


def test_attempt_limit_does_not_consume_extra_cycles() -> None:
    """无限重试应由尝试预算停止，规划轮次保持独立。"""
    loop, _, _ = build_loop()

    class AlwaysFailExecutor:
        async def execute(self, step: PlanStep, context: LoopContext) -> ExecutionResult:
            del step, context
            raise RuntimeError("always")

    class AlwaysRetry:
        def decide(self, error: Exception, attempt: int, context: LoopContext) -> RetryDecision:
            del error, attempt, context
            return RetryDecision(RetryAction.RETRY)

    loop.executors.register("default", AlwaysFailExecutor(), replace=True)
    loop.retry_policy = AlwaysRetry()
    request = LoopRequest(
        goal="bounded retry",
        limits=LoopLimits(max_cycles=5, max_attempts=2, max_steps_per_plan=5),
    )
    result = asyncio.run(loop.run(request))

    assert result.status is LoopStatus.BLOCKED
    assert result.stop_reason is StopReason.ATTEMPT_LIMIT
    assert result.cycles == 1
    assert result.total_attempts == 2


def test_resource_limit_bypasses_retry_policy() -> None:
    """计算额度不足是结构化停止条件，不得进入普通组件重试。"""
    loop, _, _ = build_loop()

    class BudgetedExecutor:
        async def execute(self, step: PlanStep, context: LoopContext) -> ExecutionResult:
            del step, context
            raise ResourceLimitExceededError("model_calls exceeded")

    class UnexpectedRetry:
        def decide(
            self,
            error: Exception,
            attempt: int,
            context: LoopContext,
        ) -> RetryDecision:
            del error, attempt, context
            raise AssertionError("resource limit must bypass retry policy")

    loop.executors.register("default", BudgetedExecutor(), replace=True)
    loop.retry_policy = UnexpectedRetry()

    result = asyncio.run(loop.run(LoopRequest("额度受限")))

    assert result.status is LoopStatus.BLOCKED
    assert result.stop_reason is StopReason.BUDGET_EXHAUSTED
    assert result.total_attempts == 1


def test_failed_verification_replans_until_cycle_limit() -> None:
    """验证反馈可以触发新轮次，但不得突破规划轮次预算。"""
    loop, _, _ = build_loop()

    class RejectingVerifier:
        async def verify(
            self,
            step: PlanStep,
            result: ExecutionResult,
            context: LoopContext,
        ) -> VerificationResult:
            del step, result, context
            return VerificationResult(False, "仍不满足", failed_criteria=("质量",))

    loop.verifiers.register("default", RejectingVerifier(), replace=True)
    request = LoopRequest(
        goal="repeated",
        limits=LoopLimits(max_cycles=2, max_attempts=10, max_steps_per_plan=5),
    )
    result = asyncio.run(loop.run(request))

    assert result.status is LoopStatus.BLOCKED
    assert result.stop_reason is StopReason.CYCLE_LIMIT
    assert result.cycles == 2
    assert result.total_attempts == 2


def test_step_selects_executor_by_name() -> None:
    """不同步骤应按自身声明动态解析执行器。"""
    loop, _, _ = build_loop()

    class NamedPlanner:
        async def plan(self, context: LoopContext) -> Plan:
            del context
            return Plan((PlanStep("work", executor="special"),))

    class SpecialExecutor:
        async def execute(self, step: PlanStep, context: LoopContext) -> ExecutionResult:
            del step, context
            return ExecutionResult("special-output")

    loop.planners.register("default", NamedPlanner(), replace=True)
    loop.executors.register("special", SpecialExecutor())

    result = asyncio.run(loop.run(LoopRequest(goal="route")))

    assert result.output == "special-output"


def test_timeout_returns_structured_stop_reason() -> None:
    """超过总运行时限后应持久化超时终态。"""
    loop, store, _ = build_loop()

    class SlowExecutor:
        async def execute(self, step: PlanStep, context: LoopContext) -> ExecutionResult:
            del step, context
            await asyncio.sleep(0.05)
            return ExecutionResult("late")

    loop.executors.register("default", SlowExecutor(), replace=True)
    request = LoopRequest(goal="timeout", limits=LoopLimits(timeout_seconds=0.001))
    result = asyncio.run(loop.run(request))

    assert result.status is LoopStatus.TIMED_OUT
    assert result.stop_reason is StopReason.TIMED_OUT
    assert store.contexts[result.run_id].status is LoopStatus.TIMED_OUT


def test_pre_cancelled_run_never_calls_components() -> None:
    """预先取消的运行应在第一个安全边界直接停止。"""
    loop, _, _ = build_loop()
    run_id = loop.create_run_id()
    assert loop.cancel(run_id)
    assert not loop.cancel(run_id)

    result = asyncio.run(loop.run(LoopRequest(goal="cancelled"), run_id=run_id))

    assert result.status is LoopStatus.CANCELLED
    assert result.stop_reason is StopReason.CANCELLED
