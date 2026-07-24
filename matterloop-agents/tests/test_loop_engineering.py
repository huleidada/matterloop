"""LoopEngineeringRuntime 工程闭环的多轮与短路行为测试。"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field

from matterloop_agents.analysis import (
    FailureCategory,
    FailureDiagnosis,
    RuleBasedFailureAnalyzer,
)
from matterloop_agents.learning import (
    EpisodeLike,
    ExperienceReuse,
    LoopEngineeringConfig,
    LoopEngineeringRuntime,
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


def _failed_result(feedback: str) -> LoopResult:
    """构造包含两条同反馈验证失败记录的失败结果。"""
    records = tuple(
        IterationRecord(
            cycle=1,
            step_index=index,
            step=PlanStep(description=f"步骤{index}"),
            execution=ExecutionResult(output=""),
            verification=VerificationResult(passed=False, feedback=feedback),
        )
        for index in range(2)
    )
    return LoopResult(
        run_id="run",
        status=LoopStatus.FAILED,
        output="",
        cycles=1,
        total_attempts=2,
        completed_steps=0,
        records=records,
        stop_reason=None,
    )


def _completed_result() -> LoopResult:
    """构造完成态结果。"""
    return LoopResult(
        run_id="run",
        status=LoopStatus.COMPLETED,
        output="完成",
        cycles=1,
        total_attempts=1,
        completed_steps=1,
        records=(),
        stop_reason=StopReason.COMPLETED,
    )


class FakeRuntime:
    """按调用顺序返回预置结果并记录请求的假运行时。"""

    def __init__(self, results: list[LoopResult]) -> None:
        self._results = list(results)
        self.requests: list[LoopRequest] = []

    async def run(self, request: LoopRequest, *, run_id: str | None = None) -> LoopResult:
        self.requests.append(request)
        return self._results.pop(0)


class FakeWriter:
    """记录每轮经验写入调用的假写入器。"""

    def __init__(self) -> None:
        self.entries: list[tuple[str, LoopStatus, FailureCategory | None]] = []

    async def record(
        self, goal: str, result: LoopResult, diagnosis: FailureDiagnosis | None
    ) -> None:
        category = diagnosis.category if diagnosis is not None else None
        self.entries.append((goal, result.status, category))


@dataclass(frozen=True)
class Episode:
    """满足 EpisodeLike 结构协议的测试经验对象。"""

    goal: str
    succeeded: bool
    failure_summary: str = ""
    resolution: str = ""
    tags: tuple[str, ...] = ()


@dataclass
class FakeSource:
    """只实现相似检索的假经验数据源。"""

    similar: tuple[Episode, ...] = ()
    calls: list[str] = field(default_factory=list)

    async def list_failures(self, limit: int) -> Sequence[EpisodeLike]:
        return ()

    async def list_successes(self, limit: int) -> Sequence[EpisodeLike]:
        return ()

    async def find_similar(self, goal: str, limit: int) -> Sequence[EpisodeLike]:
        self.calls.append(goal)
        return self.similar[:limit]


async def test_early_completion_short_circuits() -> None:
    runtime = FakeRuntime([_completed_result()])
    writer = FakeWriter()
    engineering = LoopEngineeringRuntime(runtime, RuleBasedFailureAnalyzer(), episodes=writer)
    rounds = await engineering.run(LoopRequest(goal="合成目标材料"))

    assert len(rounds) == 1
    assert rounds[0].round_index == 1
    assert rounds[0].diagnosis is None
    assert len(runtime.requests) == 1
    assert writer.entries == [("合成目标材料", LoopStatus.COMPLETED, None)]


async def test_multi_round_injects_replan_hints() -> None:
    runtime = FakeRuntime([_failed_result("补充引用来源"), _completed_result()])
    writer = FakeWriter()
    engineering = LoopEngineeringRuntime(runtime, RuleBasedFailureAnalyzer(), episodes=writer)
    request = LoopRequest(goal="撰写综述", acceptance_criteria=("包含结论",))
    rounds = await engineering.run(request)

    assert len(rounds) == 2
    assert rounds[0].diagnosis is not None
    assert rounds[0].diagnosis.category is FailureCategory.VERIFICATION_FAILURE
    assert rounds[1].diagnosis is None

    second_request = runtime.requests[1]
    assert "包含结论" in second_request.acceptance_criteria
    assert "补充引用来源" in second_request.acceptance_criteria
    # 原始请求保持不可变，不被闭环修改。
    assert request.acceptance_criteria == ("包含结论",)
    assert [entry[2] for entry in writer.entries] == [
        FailureCategory.VERIFICATION_FAILURE,
        None,
    ]


async def test_hint_injection_can_be_disabled() -> None:
    runtime = FakeRuntime([_failed_result("补充引用来源"), _completed_result()])
    engineering = LoopEngineeringRuntime(
        runtime,
        RuleBasedFailureAnalyzer(),
        config=LoopEngineeringConfig(apply_correction_hints=False),
    )
    await engineering.run(LoopRequest(goal="撰写综述", acceptance_criteria=("包含结论",)))

    assert runtime.requests[1].acceptance_criteria == ("包含结论",)


async def test_stops_at_max_rounds() -> None:
    runtime = FakeRuntime([_failed_result("反馈一"), _failed_result("反馈二")])
    engineering = LoopEngineeringRuntime(
        runtime,
        RuleBasedFailureAnalyzer(),
        config=LoopEngineeringConfig(max_rounds=2),
    )
    rounds = await engineering.run(LoopRequest(goal="撰写综述"))

    assert len(rounds) == 2
    assert all(record.diagnosis is not None for record in rounds)
    assert [record.round_index for record in rounds] == [1, 2]


async def test_recommended_path_merged_into_first_request() -> None:
    source = FakeSource(
        similar=(
            Episode(
                goal="synthesize polymer sample",
                succeeded=True,
                resolution="先检索文献再设计配方",
            ),
        )
    )
    runtime = FakeRuntime([_completed_result()])
    engineering = LoopEngineeringRuntime(
        runtime,
        RuleBasedFailureAnalyzer(),
        reuse=ExperienceReuse(source),
    )
    await engineering.run(LoopRequest(goal="synthesize polymer sample"))

    assert runtime.requests[0].metadata["recommended_path"] == "先检索文献再设计配方"
    assert source.calls == ["synthesize polymer sample"]
