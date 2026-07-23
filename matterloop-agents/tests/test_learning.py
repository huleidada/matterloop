"""失败模式聚类、策略优化与经验召回排序测试。"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field

import pytest
from matterloop_agents.evaluation import EvaluationTask, TaskKind, TaskOutcome
from matterloop_agents.learning import (
    EpisodeLike,
    ExperienceReuse,
    FailureLearningEngine,
    StrategyOptimizer,
)
from matterloop_core import (
    ExecutionResult,
    IterationRecord,
    LoopResult,
    LoopStatus,
    PlanStep,
    VerificationResult,
)


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
    """返回预置经验的假数据源。"""

    failures: tuple[Episode, ...] = ()
    successes: tuple[Episode, ...] = ()
    similar: tuple[Episode, ...] = ()
    find_similar_calls: list[tuple[str, int]] = field(default_factory=list)

    async def list_failures(self, limit: int) -> Sequence[EpisodeLike]:
        return self.failures[:limit]

    async def list_successes(self, limit: int) -> Sequence[EpisodeLike]:
        return self.successes[:limit]

    async def find_similar(self, goal: str, limit: int) -> Sequence[EpisodeLike]:
        self.find_similar_calls.append((goal, limit))
        return self.similar[:limit]


async def test_failure_pattern_clustering_and_fix_from_success() -> None:
    source = FakeSource(
        failures=(
            Episode(
                goal="write results file",
                succeeded=False,
                failure_summary="Permission denied 42 when writing file",
                tags=("fs",),
            ),
            Episode(
                goal="write summary file",
                succeeded=False,
                failure_summary="permission denied 7 when writing FILE",
                tags=("fs",),
            ),
            Episode(goal="parse output", succeeded=False, failure_summary="model output invalid"),
        ),
        successes=(
            Episode(
                goal="write file to disk",
                succeeded=True,
                resolution="以具备写权限的工作目录重试写入",
                tags=("fs",),
            ),
        ),
    )
    engine = FailureLearningEngine(source)

    summary = await engine.summarize(limit=10)
    assert max(summary.values()) == 2

    patterns = await engine.patterns(min_occurrences=2)
    assert len(patterns) == 1
    pattern = patterns[0]
    assert pattern.occurrences == 2
    assert len(pattern.example_summaries) == 2
    assert pattern.suggested_fix == "以具备写权限的工作目录重试写入"


async def test_failure_pattern_default_fix_without_matching_success() -> None:
    source = FakeSource(
        failures=(
            Episode(goal="a", succeeded=False, failure_summary="timeout while waiting"),
            Episode(goal="b", succeeded=False, failure_summary="timeout while waiting"),
        ),
    )
    patterns = await FailureLearningEngine(source).patterns(min_occurrences=2)
    assert "人工" in patterns[0].suggested_fix


def _failed_outcome(step_description: str, feedback: str) -> TaskOutcome:
    """构造一条含单个验证失败记录的任务观测。"""
    record = IterationRecord(
        cycle=1,
        step_index=0,
        step=PlanStep(description=step_description),
        execution=ExecutionResult(output=""),
        verification=VerificationResult(passed=False, feedback=feedback),
    )
    result = LoopResult(
        run_id="run",
        status=LoopStatus.FAILED,
        output="",
        cycles=1,
        total_attempts=1,
        completed_steps=0,
        records=(record,),
        stop_reason=None,
    )
    task = EvaluationTask(task_id=step_description, kind=TaskKind.BENCHMARK, goal="目标")
    return TaskOutcome(task=task, result=result, duration_seconds=0.1)


class FakeToolStats:
    """返回固定工具统计的提供者。"""

    def __init__(self, stats: Mapping[str, tuple[int, int]]) -> None:
        self._stats = stats

    def tool_stats(self) -> Mapping[str, tuple[int, int]]:
        return self._stats


def test_strategy_optimizer_plan_tool_and_prompt_suggestions() -> None:
    outcomes = (
        _failed_outcome("解析数据", "missing citation in report"),
        _failed_outcome("解析数据", "missing citation again"),
    )
    optimizer = StrategyOptimizer(tool_stats=FakeToolStats({"scraper": (1, 4), "writer": (5, 0)}))
    suggestions = optimizer.suggest(outcomes)
    kinds = [suggestion.kind for suggestion in suggestions]

    assert kinds.count("plan") == 1
    plan = next(item for item in suggestions if item.kind == "plan")
    assert "解析数据" in plan.description

    tools = [item for item in suggestions if item.kind == "tool"]
    assert len(tools) == 1
    assert "switch_tool:scraper" in tools[0].description

    prompts = [item for item in suggestions if item.kind == "prompt"]
    assert len(prompts) == 1
    assert "missing" in prompts[0].description


async def test_strategy_optimizer_from_episode_source() -> None:
    source = FakeSource(
        failures=(
            Episode(goal="a", succeeded=False, failure_summary="output schema mismatch"),
            Episode(goal="b", succeeded=False, failure_summary="schema mismatch in output"),
        ),
    )
    suggestions = await StrategyOptimizer().suggest_from_episodes(source)
    assert len(suggestions) == 1
    assert suggestions[0].kind == "prompt"
    assert "schema" in suggestions[0].description


async def test_experience_recall_reranks_by_overlap() -> None:
    low = Episode(goal="cook dinner tonight", succeeded=True, resolution="irrelevant")
    high = Episode(
        goal="write report about polymers", succeeded=True, resolution="先检索文献再汇总"
    )
    source = FakeSource(similar=(low, high))
    matches = await ExperienceReuse(source).recall("write report about polymers", limit=2)

    assert matches[0].episode is high
    assert matches[0].score > matches[1].score
    assert matches[0].score == pytest.approx(1.0)


async def test_recommend_path_returns_best_successful_resolution() -> None:
    failure = Episode(goal="write report about polymers", succeeded=False)
    success = Episode(goal="write report on metals", succeeded=True, resolution="先检索文献再汇总")
    source = FakeSource(similar=(failure, success))
    reuse = ExperienceReuse(source)

    assert await reuse.recommend_path("write report about polymers") == "先检索文献再汇总"


async def test_recommend_path_returns_none_without_success() -> None:
    source = FakeSource(similar=(Episode(goal="write report", succeeded=False),))
    assert await ExperienceReuse(source).recommend_path("write report") is None
