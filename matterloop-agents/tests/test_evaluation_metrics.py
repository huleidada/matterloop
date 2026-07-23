"""内置评测指标的数值正确性与无样本退化测试。"""

from __future__ import annotations

from collections.abc import Mapping

import pytest
from matterloop_agents.evaluation import (
    AverageCost,
    AverageExecutionTime,
    DomainAccuracy,
    EvaluationTask,
    ExecutionReliability,
    PlanningAccuracy,
    PredictionError,
    ReasoningQualityScore,
    RecoverySuccessRate,
    TaskCompletionRate,
    TaskKind,
    TaskOutcome,
    ToolSuccessRate,
    default_reasoning_scorer,
)
from matterloop_core import (
    ExecutionResult,
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
    metadata: Mapping[str, object] | None = None,
    step_index: int = 0,
) -> IterationRecord:
    """构造一条最小可用的迭代记录。"""
    return IterationRecord(
        cycle=1,
        step_index=step_index,
        step=PlanStep(description="步骤"),
        execution=ExecutionResult(output="", metadata=metadata or {}),
        verification=VerificationResult(passed=passed, feedback=feedback),
    )


def _result(
    *,
    status: LoopStatus,
    stop_reason: StopReason | None = None,
    records: tuple[IterationRecord, ...] = (),
    output: str = "",
) -> LoopResult:
    """构造一条最小可用的 Loop 终态结果。"""
    return LoopResult(
        run_id="run",
        status=status,
        output=output,
        cycles=1,
        total_attempts=max(1, len(records)),
        completed_steps=sum(1 for record in records if record.verification.passed),
        records=records,
        stop_reason=stop_reason,
    )


def _task(task_id: str, *, expected_numeric: float | None = None) -> EvaluationTask:
    """构造一条评测任务。"""
    return EvaluationTask(
        task_id=task_id,
        kind=TaskKind.BENCHMARK,
        goal=f"目标 {task_id}",
        expected_numeric=expected_numeric,
    )


def _outcomes() -> tuple[TaskOutcome, ...]:
    """构造一组一成功一失败的观测样本。"""
    completed = _result(
        status=LoopStatus.COMPLETED,
        stop_reason=StopReason.COMPLETED,
        records=(
            _record(
                passed=True,
                feedback="验证通过",
                metadata={"tool_calls": 4, "tool_failures": 1},
            ),
            _record(passed=True, step_index=1),
        ),
        output="12",
    )
    failed = _result(
        status=LoopStatus.FAILED,
        stop_reason=StopReason.COMPONENT_ERROR,
        records=(_record(passed=True, feedback="通过"), _record(passed=False, step_index=1)),
        output="0",
    )
    return (
        TaskOutcome(
            task=_task("t1", expected_numeric=10.0),
            result=completed,
            duration_seconds=1.0,
            cost_micro_units=100,
        ),
        TaskOutcome(task=_task("t2"), result=failed, duration_seconds=3.0),
    )


def test_task_completion_rate() -> None:
    result = TaskCompletionRate().compute(_outcomes())
    assert result.value == pytest.approx(0.5)
    empty = TaskCompletionRate().compute(())
    assert empty.value == 0.0
    assert empty.detail["no_samples"] is True


def test_planning_accuracy() -> None:
    result = PlanningAccuracy().compute(_outcomes())
    assert result.value == pytest.approx(3 / 4)


def test_tool_success_rate_skips_records_without_metadata() -> None:
    result = ToolSuccessRate().compute(_outcomes())
    assert result.value == pytest.approx(3 / 4)
    assert result.detail == {"tool_calls": 4, "tool_failures": 1}


def test_tool_success_rate_no_samples() -> None:
    outcome = TaskOutcome(
        task=_task("t"),
        result=_result(status=LoopStatus.COMPLETED, records=(_record(passed=True),)),
        duration_seconds=0.1,
    )
    result = ToolSuccessRate().compute((outcome,))
    assert result.value == 0.0
    assert result.detail["no_samples"] is True


def test_reasoning_quality_score_with_custom_scorer() -> None:
    metric = ReasoningQualityScore(scorer=lambda result: 0.8)
    assert metric.compute(_outcomes()).value == pytest.approx(0.8)


def test_default_reasoning_scorer_uses_feedback_ratio() -> None:
    result = _result(
        status=LoopStatus.COMPLETED,
        records=(_record(passed=True, feedback="有反馈"), _record(passed=True, step_index=1)),
    )
    assert default_reasoning_scorer(result) == pytest.approx(0.5)


def test_execution_reliability() -> None:
    result = ExecutionReliability().compute(_outcomes())
    assert result.value == pytest.approx(0.5)


def test_recovery_success_rate_degrades_without_samples() -> None:
    result = RecoverySuccessRate().compute(_outcomes())
    assert result.value == 0.0
    assert result.detail["no_samples"] is True


def test_recovery_success_rate_counts_recovered_completions() -> None:
    recovered = TaskOutcome(
        task=_task("t"),
        result=_result(status=LoopStatus.COMPLETED),
        duration_seconds=0.5,
        recovered=True,
    )
    result = RecoverySuccessRate().compute((*_outcomes(), recovered))
    assert result.value == pytest.approx(1.0)


def test_average_cost_ignores_unknown_costs() -> None:
    result = AverageCost().compute(_outcomes())
    assert result.value == pytest.approx(100.0)
    no_cost = AverageCost().compute((_outcomes()[1],))
    assert no_cost.detail["no_samples"] is True


def test_average_execution_time() -> None:
    result = AverageExecutionTime().compute(_outcomes())
    assert result.value == pytest.approx(2.0)


def test_domain_accuracy_with_injected_judge() -> None:
    metric = DomainAccuracy(judge=lambda task, result: result.output == "12")
    assert metric.compute(_outcomes()).value == pytest.approx(0.5)


def test_prediction_error_mean_absolute() -> None:
    metric = PredictionError(extractor=lambda result: float(result.output))
    result = metric.compute(_outcomes())
    assert result.value == pytest.approx(2.0)
    assert result.detail == {"samples": 1}


def test_prediction_error_degrades_without_samples() -> None:
    metric = PredictionError(extractor=lambda result: 0.0)
    result = metric.compute((_outcomes()[1],))
    assert result.value == 0.0
    assert result.detail["no_samples"] is True
