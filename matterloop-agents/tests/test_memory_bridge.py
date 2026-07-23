"""learning 与 matterloop-memory Episodic Memory 桥接的跨包集成测试。"""

from __future__ import annotations

from matterloop_agents.analysis import RuleBasedFailureAnalyzer
from matterloop_agents.learning import (
    EpisodicMemorySource,
    EpisodicMemoryWriter,
    ExperienceReuse,
    LoopEngineeringConfig,
    LoopEngineeringRuntime,
    episode_view,
)
from matterloop_core import (
    ExecutionResult,
    IterationRecord,
    LoopRequest,
    LoopResult,
    LoopStatus,
    PlanStep,
    VerificationResult,
)
from matterloop_core.state import StopReason
from matterloop_memory import EpisodeRecord, InMemoryEpisodicMemory


def _completed_result(run_id: str, output: str) -> LoopResult:
    """构造完成态结果。"""
    return LoopResult(
        run_id=run_id,
        status=LoopStatus.COMPLETED,
        output=output,
        cycles=1,
        total_attempts=1,
        completed_steps=1,
        records=(),
        stop_reason=StopReason.COMPLETED,
    )


def _failed_result(run_id: str, feedback: str) -> LoopResult:
    """构造包含验证失败记录的失败结果。"""
    record = IterationRecord(
        cycle=1,
        step_index=0,
        step=PlanStep(description="步骤"),
        execution=ExecutionResult(output=""),
        verification=VerificationResult(passed=False, feedback=feedback),
    )
    return LoopResult(
        run_id=run_id,
        status=LoopStatus.FAILED,
        output="",
        cycles=1,
        total_attempts=1,
        completed_steps=0,
        records=(record, record),
        stop_reason=None,
    )


class _FakeRuntime:
    """按调用顺序返回预置结果的假运行时。"""

    def __init__(self, results: list[LoopResult]) -> None:
        self._results = list(results)
        self.requests: list[LoopRequest] = []

    async def run(self, request: LoopRequest, *, run_id: str | None = None) -> LoopResult:
        self.requests.append(request)
        return self._results.pop(0)


def test_episode_view_projects_status_and_optional_fields() -> None:
    """视图应把终态映射为布尔成功标记并把空字段规约为空字符串。"""
    record = EpisodeRecord(
        run_id="run-1",
        goal="生成月度报告",
        status=LoopStatus.FAILED,
        failure_summary=None,
        resolution=None,
        tags=("verification_failure",),
    )
    view = episode_view(record)

    assert view.succeeded is False
    assert view.failure_summary == ""
    assert view.resolution == ""
    assert view.tags == ("verification_failure",)


async def test_writer_records_success_and_failure_rounds() -> None:
    """写入器应把成功轮次的输出沉淀为可复用 resolution，失败轮次带诊断标签。"""
    store = InMemoryEpisodicMemory()
    writer = EpisodicMemoryWriter(store)
    analyzer = RuleBasedFailureAnalyzer()

    failed = _failed_result("run-f", "缺少引用来源")
    diagnosis = await analyzer.analyze(failed)
    await writer.record("撰写调研摘要", failed, diagnosis)
    await writer.record("撰写调研摘要", _completed_result("run-s", "按三段式完成摘要"), None)

    failures = await store.list_failures(10)
    successes = await store.list_successes(10)
    assert len(failures) == 1
    assert failures[0].failure_summary == diagnosis.summary
    assert failures[0].tags == (diagnosis.category.value,)
    assert len(successes) == 1
    assert successes[0].resolution == "按三段式完成摘要"
    assert successes[0].tags == ("success",)


async def test_experience_reuse_recommends_path_from_memory_store() -> None:
    """经验复用应能通过桥接数据源召回记忆包中的成功路径。"""
    store = InMemoryEpisodicMemory()
    await store.record(
        EpisodeRecord(
            run_id="run-old",
            goal="生成季度发布说明",
            status=LoopStatus.COMPLETED,
            resolution="先列变更清单再逐项验证",
        )
    )
    reuse = ExperienceReuse(EpisodicMemorySource(store))

    path = await reuse.recommend_path("生成季度发布说明")

    assert path == "先列变更清单再逐项验证"


async def test_loop_engineering_runtime_end_to_end_with_memory() -> None:
    """工程闭环应把首轮失败经验入库并在下一轮携带纠正提示后完成。"""
    store = InMemoryEpisodicMemory()
    runtime = _FakeRuntime(
        [
            _failed_result("round-1", "结论缺少验证依据"),
            _completed_result("round-2", "已补充验证依据"),
        ]
    )
    engineering = LoopEngineeringRuntime(
        runtime,
        RuleBasedFailureAnalyzer(),
        episodes=EpisodicMemoryWriter(store),
        reuse=ExperienceReuse(EpisodicMemorySource(store)),
        config=LoopEngineeringConfig(max_rounds=3),
    )

    rounds = await engineering.run(LoopRequest(goal="生成发布说明并自检"))

    assert len(rounds) == 2
    assert rounds[0].diagnosis is not None
    assert rounds[1].result.status is LoopStatus.COMPLETED
    # 第二轮请求应包含来自诊断的纠正提示。
    assert len(runtime.requests[1].acceptance_criteria) > len(
        runtime.requests[0].acceptance_criteria
    )
    # 失败与成功经验都已写入长期记忆。
    assert len(await store.list_failures(10)) == 1
    assert len(await store.list_successes(10)) == 1
