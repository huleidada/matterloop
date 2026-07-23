"""EvaluationRunner 全流程与钩子调用顺序测试。"""

from __future__ import annotations

import pytest
from matterloop_agents.evaluation import (
    EvaluationDataset,
    EvaluationReport,
    EvaluationRunner,
    EvaluationTask,
    TaskCompletionRate,
    TaskKind,
    TaskOutcome,
)
from matterloop_core import LoopRequest, LoopResult, LoopStatus
from matterloop_core.state import StopReason


def _result(status: LoopStatus) -> LoopResult:
    """构造一条最小可用的 Loop 终态结果。"""
    return LoopResult(
        run_id="run",
        status=status,
        output="ok" if status is LoopStatus.COMPLETED else "",
        cycles=1,
        total_attempts=1,
        completed_steps=0,
        records=(),
        stop_reason=(
            StopReason.COMPLETED if status is LoopStatus.COMPLETED else StopReason.COMPONENT_ERROR
        ),
    )


class FakeRuntime:
    """按调用顺序返回预置结果并记录请求的假运行时。"""

    def __init__(self, results: list[LoopResult]) -> None:
        self._results = list(results)
        self.requests: list[LoopRequest] = []

    async def run(self, request: LoopRequest, *, run_id: str | None = None) -> LoopResult:
        self.requests.append(request)
        return self._results.pop(0)


def _dataset() -> EvaluationDataset:
    """构造含重复 task_id 的三条任务数据集。"""
    return EvaluationDataset(
        [
            EvaluationTask(
                task_id="t1",
                kind=TaskKind.BENCHMARK,
                goal="计算材料带隙",
                acceptance_criteria=("给出数值",),
            ),
            EvaluationTask(task_id="t2", kind=TaskKind.GOLDEN, goal="生成实验报告"),
            EvaluationTask(task_id="t1", kind=TaskKind.REGRESSION, goal="重复任务应被去重"),
        ]
    )


async def test_full_flow_and_hook_order() -> None:
    calls: list[str] = []

    async def memory_hook(outcome: TaskOutcome) -> None:
        calls.append(f"memory:{outcome.task.task_id}")

    async def improve_hook(report: EvaluationReport) -> None:
        calls.append(f"improve:{len(report.outcomes)}")

    runtime = FakeRuntime([_result(LoopStatus.COMPLETED), _result(LoopStatus.FAILED)])
    runner = EvaluationRunner(
        runtime,
        [TaskCompletionRate()],
        improve_hook=improve_hook,
        memory_hook=memory_hook,
    )
    report = await runner.evaluate(_dataset())

    # 重复 task_id 去重后只执行两条任务；memory 钩子逐任务先行，improve 钩子最后一次。
    assert calls == ["memory:t1", "memory:t2", "improve:2"]
    assert len(report.outcomes) == 2
    assert report.outcomes[0].task.task_id == "t1"
    assert report.outcomes[0].duration_seconds >= 0
    assert report.metrics[0].name == "task_completion_rate"
    assert report.metrics[0].value == pytest.approx(0.5)
    assert report.finished_at >= report.started_at


async def test_request_construction_from_task() -> None:
    runtime = FakeRuntime([_result(LoopStatus.COMPLETED), _result(LoopStatus.COMPLETED)])
    runner = EvaluationRunner(runtime, [])
    await runner.evaluate(_dataset())

    request = runtime.requests[0]
    assert request.goal == "计算材料带隙"
    assert request.acceptance_criteria == ("给出数值",)
    assert request.metadata["evaluation_task_id"] == "t1"
    assert request.metadata["evaluation_task_kind"] == TaskKind.BENCHMARK.value


async def test_kind_filter_limits_executed_tasks() -> None:
    runtime = FakeRuntime([_result(LoopStatus.COMPLETED)])
    runner = EvaluationRunner(runtime, [TaskCompletionRate()])
    report = await runner.evaluate(_dataset(), kinds=[TaskKind.GOLDEN])

    assert len(report.outcomes) == 1
    assert report.outcomes[0].task.task_id == "t2"
    assert report.metrics[0].value == pytest.approx(1.0)


def test_dataset_filter_by_tags() -> None:
    dataset = EvaluationDataset(
        [
            EvaluationTask(
                task_id="a", kind=TaskKind.BENCHMARK, goal="目标A", domain_tags=("polymer",)
            ),
            EvaluationTask(task_id="b", kind=TaskKind.BENCHMARK, goal="目标B"),
        ]
    )
    assert [task.task_id for task in dataset.filter(tags=["polymer"])] == ["a"]
    assert len(dataset) == 2
