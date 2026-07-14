"""预算、停止和重试策略测试。"""

import asyncio
import random

from matterloop_core import (
    ApprovalDecision,
    ExecutionResult,
    IterationRecord,
    LoopContext,
    LoopRequest,
    PlanStep,
    RetryAction,
    VerificationResult,
)
from matterloop_policies import (
    ApprovalRule,
    BudgetLimits,
    BudgetPolicy,
    CompositeLoopPolicy,
    ExponentialBackoffRetryPolicy,
    NoProgressStopPolicy,
    PermissionRule,
    RetryConfig,
    RuleBasedApprovalGate,
    RuleBasedPermissionPolicy,
    StopConfig,
    UsageLedger,
)
from matterloop_tools import PermissionDecision, ToolContext


def test_budget_policy_reads_usage_ledger() -> None:
    """达到工具调用预算后应阻止继续。"""
    context = LoopContext(LoopRequest(goal="budget"))
    ledger = UsageLedger()
    policy = BudgetPolicy(BudgetLimits(max_tool_calls=1), ledger)

    assert policy.can_continue(context)
    ledger.record_tool_call(context.run_id)
    assert not policy.can_continue(context)


def test_retry_policy_is_bounded() -> None:
    """可重试异常达到次数上限后应快速失败。"""
    context = LoopContext(LoopRequest(goal="retry"))
    policy = ExponentialBackoffRetryPolicy(
        RetryConfig(max_attempts=2, jitter_ratio=0),
        random_source=random.Random(1),
    )

    assert policy.decide(TimeoutError(), 1, context).action is RetryAction.RETRY
    assert policy.decide(TimeoutError(), 2, context).action is RetryAction.FAIL


def test_rule_based_approval_defaults_to_deferred() -> None:
    """未命中审批规则时不能隐式放行高风险步骤。"""

    async def scenario() -> None:
        gate = RuleBasedApprovalGate((ApprovalRule("shell", ApprovalDecision.APPROVED),))
        context = LoopContext(LoopRequest("审批命令"))

        assert (
            await gate.decide(PlanStep("执行测试", executor="shell"), context)
            is ApprovalDecision.APPROVED
        )
        assert (
            await gate.decide(PlanStep("写入文件", executor="filesystem"), context)
            is ApprovalDecision.DEFERRED
        )

    asyncio.run(scenario())


def test_permission_policy_matches_tool_and_operation() -> None:
    """工具写操作需要显式规则，未知操作保持拒绝。"""

    async def scenario() -> None:
        policy = RuleBasedPermissionPolicy(
            (
                PermissionRule(
                    "filesystem",
                    ("read",),
                    PermissionDecision.ALLOW,
                ),
            )
        )
        context = ToolContext("run-1")

        assert (
            await policy.authorize("filesystem", {"operation": "read"}, context)
            is PermissionDecision.ALLOW
        )
        assert (
            await policy.authorize("filesystem", {"operation": "write"}, context)
            is PermissionDecision.DENY
        )

    asyncio.run(scenario())


def test_no_progress_and_composite_policy_stop_repeated_feedback() -> None:
    """连续相同失败反馈达到阈值时组合策略应停止。"""
    context = LoopContext(LoopRequest("避免无效循环"))
    step = PlanStep("执行验证")
    for cycle in (1, 2):
        context.records.append(
            IterationRecord(
                cycle=cycle,
                step_index=0,
                step=step,
                execution=ExecutionResult("未通过"),
                verification=VerificationResult(False, "同一问题"),
                attempt=1,
            )
        )
    no_progress = NoProgressStopPolicy(StopConfig(max_identical_feedback=2))
    budget = BudgetPolicy(BudgetLimits(max_tool_calls=10), UsageLedger())

    assert not no_progress.can_continue(context)
    assert not CompositeLoopPolicy(budget, no_progress).can_continue(context)


def test_usage_ledger_tracks_each_dimension_without_context_metadata() -> None:
    """用量账本应独立累计模型、工具和执行次数。"""
    ledger = UsageLedger()
    ledger.record_model_usage("run-usage", input_tokens=10, output_tokens=4, cost_micros=3)
    ledger.record_tool_call("run-usage")
    ledger.record_attempt("run-usage")

    snapshot = ledger.snapshot("run-usage")

    assert snapshot.input_tokens == 10
    assert snapshot.output_tokens == 4
    assert snapshot.cost_micros == 3
    assert snapshot.tool_calls == 1
    assert snapshot.attempts == 1
    ledger.clear("run-usage")
    assert ledger.snapshot("run-usage").tool_calls == 0
