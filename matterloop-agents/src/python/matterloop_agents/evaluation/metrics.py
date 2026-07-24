"""评测结果值对象与 Agent、Runtime、领域三类内置指标。"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from math import isfinite
from types import MappingProxyType
from typing import Protocol

from matterloop_core import IterationRecord, LoopResult, LoopStatus
from matterloop_core.state import StopReason

from matterloop_agents.evaluation.dataset import EvaluationTask

_NO_SAMPLES_DETAIL: Mapping[str, object] = MappingProxyType({"no_samples": True})


@dataclass(frozen=True, slots=True)
class TaskOutcome:
    """一条评测任务执行完成后的完整观测。

    Args:
        task: 被执行的评测任务。
        result: Loop 运行的不可变终态结果。
        duration_seconds: 本次执行耗时（秒）。
        cost_micro_units: 可选的成本（微单位），未知时为 ``None``。
        recovered: 本次执行是否经历过恢复流程。
    """

    task: EvaluationTask
    result: LoopResult
    duration_seconds: float
    cost_micro_units: int | None = None
    recovered: bool = False

    def __post_init__(self) -> None:
        """拒绝无法解释的耗时观测。"""
        if not isfinite(self.duration_seconds) or self.duration_seconds < 0:
            raise ValueError("duration_seconds must be finite and non-negative")


@dataclass(frozen=True, slots=True)
class MetricResult:
    """一个指标计算完成后的数值与解释信息。

    Args:
        name: 指标名称。
        value: 指标数值。
        detail: 只读的计算过程解释数据。
    """

    name: str
    value: float
    detail: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        """校验指标标识并冻结解释数据。"""
        if not self.name.strip():
            raise ValueError("name must not be empty")
        object.__setattr__(self, "detail", MappingProxyType(dict(self.detail)))


class EvaluationMetric(Protocol):
    """把任务观测序列聚合为单一数值的指标接口。"""

    @property
    def name(self) -> str:
        """返回指标的稳定名称。"""
        ...

    def compute(self, outcomes: Sequence[TaskOutcome]) -> MetricResult:
        """基于全部任务观测计算指标数值。"""
        ...


def _no_samples(name: str) -> MetricResult:
    """构造无样本时的退化指标结果。"""
    return MetricResult(name=name, value=0.0, detail=_NO_SAMPLES_DETAIL)


def _ratio(name: str, hits: int, total: int) -> MetricResult:
    """构造带命中计数解释的比例型指标结果。"""
    if total == 0:
        return _no_samples(name)
    return MetricResult(name=name, value=hits / total, detail={"hits": hits, "total": total})


def default_tool_usage_extractor(record: IterationRecord) -> tuple[int, int] | None:
    """从执行元数据读取 ``tool_calls`` 与 ``tool_failures`` 计数。

    Args:
        record: 一条迭代记录。

    Returns:
        ``(调用次数, 失败次数)``；元数据缺失或类型不符时返回 ``None`` 表示跳过。
    """
    calls = record.execution.metadata.get("tool_calls")
    failures = record.execution.metadata.get("tool_failures")
    if not isinstance(calls, int) or not isinstance(failures, int):
        return None
    if calls < 0 or failures < 0 or failures > calls:
        return None
    return calls, failures


def default_reasoning_scorer(result: LoopResult) -> float:
    """按验证反馈非空比例启发式评估推理质量。

    Args:
        result: Loop 运行结果。

    Returns:
        0 到 1 之间的启发式评分；没有迭代记录时返回 0。
    """
    if not result.records:
        return 0.0
    informative = sum(1 for record in result.records if record.verification.feedback.strip())
    return informative / len(result.records)


class TaskCompletionRate:
    """统计终态为 ``COMPLETED`` 的任务占比。"""

    @property
    def name(self) -> str:
        """返回指标名称。"""
        return "task_completion_rate"

    def compute(self, outcomes: Sequence[TaskOutcome]) -> MetricResult:
        """计算完成率。"""
        hits = sum(1 for outcome in outcomes if outcome.result.status is LoopStatus.COMPLETED)
        return _ratio(self.name, hits, len(outcomes))


class PlanningAccuracy:
    """统计全部迭代记录中验证通过步骤的占比。"""

    @property
    def name(self) -> str:
        """返回指标名称。"""
        return "planning_accuracy"

    def compute(self, outcomes: Sequence[TaskOutcome]) -> MetricResult:
        """计算验证通过比例。"""
        records = [record for outcome in outcomes for record in outcome.result.records]
        passed = sum(1 for record in records if record.verification.passed)
        return _ratio(self.name, passed, len(records))


class ToolSuccessRate:
    """按注入的提取函数统计工具调用成功率。

    Args:
        extractor: 从迭代记录提取 ``(调用次数, 失败次数)`` 的函数；返回 ``None`` 时跳过该记录。
    """

    def __init__(
        self,
        extractor: Callable[[IterationRecord], tuple[int, int] | None] = (
            default_tool_usage_extractor
        ),
    ) -> None:
        self._extractor = extractor

    @property
    def name(self) -> str:
        """返回指标名称。"""
        return "tool_success_rate"

    def compute(self, outcomes: Sequence[TaskOutcome]) -> MetricResult:
        """聚合全部记录的工具调用计数并计算成功率。"""
        total_calls = 0
        total_failures = 0
        for outcome in outcomes:
            for record in outcome.result.records:
                usage = self._extractor(record)
                if usage is None:
                    continue
                calls, failures = usage
                total_calls += calls
                total_failures += failures
        if total_calls == 0:
            return _no_samples(self.name)
        return MetricResult(
            name=self.name,
            value=(total_calls - total_failures) / total_calls,
            detail={"tool_calls": total_calls, "tool_failures": total_failures},
        )


class ReasoningQualityScore:
    """按注入的评分函数评估推理质量并取平均值。

    Args:
        scorer: 输入 Loop 结果、输出 0 到 1 评分的函数。
    """

    def __init__(self, scorer: Callable[[LoopResult], float] = default_reasoning_scorer) -> None:
        self._scorer = scorer

    @property
    def name(self) -> str:
        """返回指标名称。"""
        return "reasoning_quality_score"

    def compute(self, outcomes: Sequence[TaskOutcome]) -> MetricResult:
        """计算全部任务的平均推理质量评分。"""
        if not outcomes:
            return _no_samples(self.name)
        scores = [self._scorer(outcome.result) for outcome in outcomes]
        return MetricResult(
            name=self.name, value=sum(scores) / len(scores), detail={"samples": len(scores)}
        )


class ExecutionReliability:
    """统计未发生组件错误且未进入失败终态的任务占比。"""

    @property
    def name(self) -> str:
        """返回指标名称。"""
        return "execution_reliability"

    def compute(self, outcomes: Sequence[TaskOutcome]) -> MetricResult:
        """计算执行可靠性占比。"""
        hits = sum(
            1
            for outcome in outcomes
            if outcome.result.stop_reason is not StopReason.COMPONENT_ERROR
            and outcome.result.status is not LoopStatus.FAILED
        )
        return _ratio(self.name, hits, len(outcomes))


class RecoverySuccessRate:
    """统计经历恢复的任务中最终完成的占比。"""

    @property
    def name(self) -> str:
        """返回指标名称。"""
        return "recovery_success_rate"

    def compute(self, outcomes: Sequence[TaskOutcome]) -> MetricResult:
        """计算恢复成功率；没有恢复样本时退化为 0 并注明。"""
        recovered = [outcome for outcome in outcomes if outcome.recovered]
        hits = sum(1 for outcome in recovered if outcome.result.status is LoopStatus.COMPLETED)
        return _ratio(self.name, hits, len(recovered))


class AverageCost:
    """统计已知成本样本的平均成本（微单位）。"""

    @property
    def name(self) -> str:
        """返回指标名称。"""
        return "average_cost"

    def compute(self, outcomes: Sequence[TaskOutcome]) -> MetricResult:
        """计算平均成本；没有成本样本时退化为 0 并注明。"""
        costs = [
            outcome.cost_micro_units for outcome in outcomes if outcome.cost_micro_units is not None
        ]
        if not costs:
            return _no_samples(self.name)
        return MetricResult(
            name=self.name, value=sum(costs) / len(costs), detail={"samples": len(costs)}
        )


class AverageExecutionTime:
    """统计任务平均执行耗时（秒）。"""

    @property
    def name(self) -> str:
        """返回指标名称。"""
        return "average_execution_time"

    def compute(self, outcomes: Sequence[TaskOutcome]) -> MetricResult:
        """计算平均耗时；没有样本时退化为 0 并注明。"""
        if not outcomes:
            return _no_samples(self.name)
        durations = [outcome.duration_seconds for outcome in outcomes]
        return MetricResult(
            name=self.name,
            value=sum(durations) / len(durations),
            detail={"samples": len(durations)},
        )


class DomainAccuracy:
    """按注入的领域判定函数统计正确任务占比。

    Args:
        judge: 输入任务与 Loop 结果、输出是否正确的判定函数。
    """

    def __init__(self, judge: Callable[[EvaluationTask, LoopResult], bool]) -> None:
        self._judge = judge

    @property
    def name(self) -> str:
        """返回指标名称。"""
        return "domain_accuracy"

    def compute(self, outcomes: Sequence[TaskOutcome]) -> MetricResult:
        """计算领域正确率。"""
        hits = sum(1 for outcome in outcomes if self._judge(outcome.task, outcome.result))
        return _ratio(self.name, hits, len(outcomes))


class PredictionError:
    """统计期望数值与提取数值之间的平均绝对误差。

    Args:
        extractor: 从 Loop 结果提取预测数值的函数。
    """

    def __init__(self, extractor: Callable[[LoopResult], float]) -> None:
        self._extractor = extractor

    @property
    def name(self) -> str:
        """返回指标名称。"""
        return "prediction_error"

    def compute(self, outcomes: Sequence[TaskOutcome]) -> MetricResult:
        """计算平均绝对误差；没有带期望数值的样本时退化为 0 并注明。"""
        errors: list[float] = []
        for outcome in outcomes:
            expected = outcome.task.expected_numeric
            if expected is None:
                continue
            errors.append(abs(self._extractor(outcome.result) - expected))
        if not errors:
            return _no_samples(self.name)
        return MetricResult(
            name=self.name, value=sum(errors) / len(errors), detail={"samples": len(errors)}
        )


__all__ = [
    "AverageCost",
    "AverageExecutionTime",
    "DomainAccuracy",
    "EvaluationMetric",
    "ExecutionReliability",
    "MetricResult",
    "PlanningAccuracy",
    "PredictionError",
    "ReasoningQualityScore",
    "RecoverySuccessRate",
    "TaskCompletionRate",
    "TaskOutcome",
    "ToolSuccessRate",
    "default_reasoning_scorer",
    "default_tool_usage_extractor",
]
