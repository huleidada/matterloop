"""评测闭环：Task→Execute→Evaluate→Score→Improve→Update Memory。"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from time import perf_counter
from typing import Protocol

from matterloop_core import LoopRequest, LoopResult

from matterloop_agents.evaluation.dataset import EvaluationDataset, EvaluationTask, TaskKind
from matterloop_agents.evaluation.metrics import EvaluationMetric, MetricResult, TaskOutcome

ImproveHook = Callable[["EvaluationReport"], Awaitable[None]]
MemoryHook = Callable[[TaskOutcome], Awaitable[None]]


class LoopRuntimeLike(Protocol):
    """评测所需的最小异步 Loop 运行接口。

    该结构协议与 ``collaboration.endpoint.LoopRuntime`` 的方法签名保持一致，
    避免本包反向依赖具体运行时实现；调用方可注入任意运行时或测试替身。
    """

    async def run(self, request: LoopRequest, *, run_id: str | None = None) -> LoopResult:
        """执行一次 Loop 并返回结构化结果。"""
        ...


@dataclass(frozen=True, slots=True)
class EvaluationReport:
    """一轮评测完成后的全部观测与指标结论。

    Args:
        outcomes: 逐任务的执行观测。
        metrics: 全部指标的计算结果。
        started_at: 评测开始时间。
        finished_at: 评测结束时间。
    """

    outcomes: tuple[TaskOutcome, ...]
    metrics: tuple[MetricResult, ...]
    started_at: datetime
    finished_at: datetime


class EvaluationRunner:
    """驱动评测闭环的执行器。

    Args:
        runtime: 用于执行任务的异步 Loop 运行时。
        metrics: 参与计分的指标序列。
        improve_hook: 可选的改进钩子，在整轮评测计分后调用一次。
        memory_hook: 可选的记忆钩子，在每个任务完成后调用，用于经验入库。
    """

    def __init__(
        self,
        runtime: LoopRuntimeLike,
        metrics: Sequence[EvaluationMetric],
        *,
        improve_hook: ImproveHook | None = None,
        memory_hook: MemoryHook | None = None,
    ) -> None:
        self._runtime = runtime
        self._metrics = tuple(metrics)
        self._improve_hook = improve_hook
        self._memory_hook = memory_hook

    async def evaluate(
        self,
        dataset: EvaluationDataset,
        *,
        kinds: Iterable[TaskKind] | None = None,
    ) -> EvaluationReport:
        """逐任务执行数据集并产出评测报告。

        Args:
            dataset: 待执行的评测任务数据集。
            kinds: 可选的任务分类过滤条件。

        Returns:
            含逐任务观测、指标结果与起止时间的评测报告。
        """
        started_at = datetime.now(timezone.utc)
        outcomes: list[TaskOutcome] = []
        for task in dataset.filter(kinds=kinds):
            outcome = await self._run_task(task)
            outcomes.append(outcome)
            # 每个任务完成后立即入库，保证经验不因后续任务失败而丢失。
            if self._memory_hook is not None:
                await self._memory_hook(outcome)
        metric_results = tuple(metric.compute(outcomes) for metric in self._metrics)
        report = EvaluationReport(
            outcomes=tuple(outcomes),
            metrics=metric_results,
            started_at=started_at,
            finished_at=datetime.now(timezone.utc),
        )
        if self._improve_hook is not None:
            await self._improve_hook(report)
        return report

    async def _run_task(self, task: EvaluationTask) -> TaskOutcome:
        """把评测任务映射为 Loop 请求执行并计时。"""
        request = LoopRequest(
            goal=task.goal,
            acceptance_criteria=task.acceptance_criteria,
            metadata={
                **dict(task.metadata),
                "evaluation_task_id": task.task_id,
                "evaluation_task_kind": task.kind.value,
            },
        )
        started = perf_counter()
        result = await self._runtime.run(request)
        duration = perf_counter() - started
        return TaskOutcome(task=task, result=result, duration_seconds=duration)


__all__ = [
    "EvaluationReport",
    "EvaluationRunner",
    "ImproveHook",
    "LoopRuntimeLike",
    "MemoryHook",
]
