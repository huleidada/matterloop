"""RuleBasedFailureAnalyzer 的规则命中与模式注入测试。"""

from __future__ import annotations

from collections.abc import Mapping

from matterloop_agents.analysis import FailureCategory, RuleBasedFailureAnalyzer
from matterloop_core import (
    ExecutionResult,
    HumanAction,
    HumanInteractionKind,
    HumanInteractionRecord,
    HumanInteractionRequest,
    HumanResponse,
    IterationRecord,
    LoopResult,
    LoopStatus,
    PlanStep,
    VerificationResult,
)
from matterloop_core.state import StopReason


def _record(
    *,
    passed: bool,
    feedback: str = "",
    output: str = "",
    metadata: Mapping[str, object] | None = None,
    description: str = "执行步骤",
    step_index: int = 0,
) -> IterationRecord:
    """构造一条最小可用的迭代记录。"""
    return IterationRecord(
        cycle=1,
        step_index=step_index,
        step=PlanStep(description=description),
        execution=ExecutionResult(output=output, metadata=metadata or {}),
        verification=VerificationResult(passed=passed, feedback=feedback),
    )


def _result(
    *,
    status: LoopStatus = LoopStatus.FAILED,
    stop_reason: StopReason | None = None,
    records: tuple[IterationRecord, ...] = (),
    error: str = "",
    human_interactions: tuple[HumanInteractionRecord, ...] = (),
) -> LoopResult:
    """构造一条最小可用的 Loop 终态结果。"""
    return LoopResult(
        run_id="run",
        status=status,
        output=records[-1].execution.output if records else "",
        cycles=1,
        total_attempts=max(1, len(records)),
        completed_steps=sum(1 for record in records if record.verification.passed),
        records=records,
        stop_reason=stop_reason,
        error=error,
        human_interactions=human_interactions,
    )


async def test_budget_exhausted_rule() -> None:
    analyzer = RuleBasedFailureAnalyzer()
    for reason in (StopReason.BUDGET_EXHAUSTED, StopReason.CYCLE_LIMIT, StopReason.ATTEMPT_LIMIT):
        diagnosis = await analyzer.analyze(_result(stop_reason=reason))
        assert diagnosis.category is FailureCategory.BUDGET_EXHAUSTED
        assert "increase_budget" in diagnosis.strategy.suggested_actions


async def test_human_rejected_rule_collects_feedback() -> None:
    request = HumanInteractionRequest(
        kind=HumanInteractionKind.APPROVAL, prompt="是否批准该计划？", interaction_id="i1"
    )
    response = HumanResponse(interaction_id="i1", action=HumanAction.REJECT, content="请改用方案B")
    result = _result(
        stop_reason=StopReason.HUMAN_REJECTED,
        human_interactions=(HumanInteractionRecord(request=request, response=response),),
    )
    diagnosis = await RuleBasedFailureAnalyzer().analyze(result)
    assert diagnosis.category is FailureCategory.HUMAN_REJECTED
    assert "请改用方案B" in diagnosis.evidence
    assert "请改用方案B" in diagnosis.strategy.replan_hints


async def test_timeout_stop_reason_rule() -> None:
    result = _result(status=LoopStatus.TIMED_OUT, stop_reason=StopReason.TIMED_OUT)
    diagnosis = await RuleBasedFailureAnalyzer().analyze(result)
    assert diagnosis.category is FailureCategory.TIMEOUT


async def test_step_limit_maps_to_planner_error() -> None:
    result = _result(stop_reason=StopReason.STEP_LIMIT)
    diagnosis = await RuleBasedFailureAnalyzer().analyze(result)
    assert diagnosis.category is FailureCategory.PLANNER_ERROR


async def test_repeated_verification_failures_rule() -> None:
    records = (
        _record(passed=False, feedback="缺少单元测试", step_index=0),
        _record(passed=False, feedback="输出格式不对", step_index=1),
    )
    diagnosis = await RuleBasedFailureAnalyzer().analyze(_result(records=records))
    assert diagnosis.category is FailureCategory.VERIFICATION_FAILURE
    assert "缺少单元测试" in diagnosis.strategy.replan_hints
    assert "输出格式不对" in diagnosis.strategy.replan_hints
    assert any("缺少单元测试" in item for item in diagnosis.evidence)


async def test_default_pattern_table_mappings() -> None:
    analyzer = RuleBasedFailureAnalyzer()
    expectations = {
        "permission denied: /etc/hosts": FailureCategory.ENVIRONMENT_ERROR,
        "target file not found": FailureCategory.PARAMETER_ERROR,
        "operation timeout while waiting": FailureCategory.TIMEOUT,
        "invalid argument for tool call": FailureCategory.TOOL_FAILURE,
    }
    for error, category in expectations.items():
        diagnosis = await analyzer.analyze(_result(error=error))
        assert diagnosis.category is category, error


async def test_pattern_matches_failed_record_texts() -> None:
    records = (_record(passed=False, output="Permission Denied when writing"),)
    diagnosis = await RuleBasedFailureAnalyzer().analyze(_result(records=records))
    assert diagnosis.category is FailureCategory.ENVIRONMENT_ERROR


async def test_injected_patterns_take_precedence() -> None:
    analyzer = RuleBasedFailureAnalyzer(extra_patterns={"not found": FailureCategory.TOOL_FAILURE})
    diagnosis = await analyzer.analyze(_result(error="executable not found"))
    assert diagnosis.category is FailureCategory.TOOL_FAILURE


async def test_empty_plan_maps_to_planner_error() -> None:
    diagnosis = await RuleBasedFailureAnalyzer().analyze(_result(records=()))
    assert diagnosis.category is FailureCategory.PLANNER_ERROR


async def test_unknown_fallback() -> None:
    records = (_record(passed=True, feedback="通过"),)
    result = _result(
        stop_reason=StopReason.COMPONENT_ERROR, records=records, error="mysterious explosion"
    )
    diagnosis = await RuleBasedFailureAnalyzer().analyze(result)
    assert diagnosis.category is FailureCategory.UNKNOWN
    assert diagnosis.strategy.confidence <= 0.5
