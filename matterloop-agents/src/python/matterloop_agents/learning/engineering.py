"""完整工程闭环 Runtime：Goal→Plan→Execute→Verify→Learn→Memory Update→Next Loop。"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Protocol

from matterloop_core import LoopRequest, LoopResult, LoopStatus

from matterloop_agents.analysis.models import FailureAnalyzer, FailureDiagnosis
from matterloop_agents.evaluation.loop import LoopRuntimeLike
from matterloop_agents.learning.reuse import ExperienceReuse


@dataclass(frozen=True, slots=True)
class LoopEngineeringConfig:
    """工程闭环的运行配置。

    Args:
        max_rounds: 最多执行的 Loop 轮数。
        apply_correction_hints: 是否把纠正提示并入下一轮请求。
    """

    max_rounds: int = 3
    apply_correction_hints: bool = True

    def __post_init__(self) -> None:
        """拒绝无法形成有效闭环的轮次配置。"""
        if self.max_rounds < 1:
            raise ValueError("max_rounds must be at least 1")


class EpisodeWriter(Protocol):
    """把每轮执行经验写入记忆的最小接口。"""

    async def record(
        self, goal: str, result: LoopResult, diagnosis: FailureDiagnosis | None
    ) -> None:
        """记录一轮执行的目标、结果与可选诊断。"""
        ...


@dataclass(frozen=True, slots=True)
class RoundRecord:
    """闭环中一轮执行的不可变记录。

    Args:
        round_index: 从 1 开始的轮次序号。
        result: 该轮 Loop 的终态结果。
        diagnosis: 该轮的失败诊断；完成轮次为 ``None``。
    """

    round_index: int
    result: LoopResult
    diagnosis: FailureDiagnosis | None


class LoopEngineeringRuntime:
    """驱动多轮"执行—诊断—学习—重试"闭环的运行时。

    Args:
        runtime: 用于执行单轮 Loop 的异步运行时。
        analyzer: 失败分析器。
        episodes: 可选的经验写入器，每轮结束后记录经验。
        reuse: 可选的经验复用器，用于在首轮注入推荐路径。
        config: 闭环运行配置。
    """

    def __init__(
        self,
        runtime: LoopRuntimeLike,
        analyzer: FailureAnalyzer,
        *,
        episodes: EpisodeWriter | None = None,
        reuse: ExperienceReuse | None = None,
        config: LoopEngineeringConfig | None = None,
    ) -> None:
        self._runtime = runtime
        self._analyzer = analyzer
        self._episodes = episodes
        self._reuse = reuse
        self._config = config or LoopEngineeringConfig()

    async def run(self, request: LoopRequest) -> tuple[RoundRecord, ...]:
        """执行完整工程闭环并返回全部轮次记录。

        Args:
            request: 初始 Loop 请求。

        Returns:
            按执行顺序排列的轮次记录；完成轮次的诊断为 ``None``。
        """
        current = await self._with_recommended_path(request)
        rounds: list[RoundRecord] = []
        for round_index in range(1, self._config.max_rounds + 1):
            result = await self._runtime.run(current)
            # 完成即短路：记录成功经验后立即结束闭环。
            if result.status is LoopStatus.COMPLETED:
                await self._write_episode(current.goal, result, None)
                rounds.append(RoundRecord(round_index=round_index, result=result, diagnosis=None))
                break
            diagnosis = await self._analyzer.analyze(result)
            await self._write_episode(current.goal, result, diagnosis)
            rounds.append(RoundRecord(round_index=round_index, result=result, diagnosis=diagnosis))
            if round_index < self._config.max_rounds:
                current = self._next_request(current, diagnosis)
        return tuple(rounds)

    async def _with_recommended_path(self, request: LoopRequest) -> LoopRequest:
        """把相似成功经验的推荐路径并入首轮请求元数据。"""
        if self._reuse is None:
            return request
        path = await self._reuse.recommend_path(request.goal)
        if path is None:
            return request
        return replace(
            request,
            metadata={**dict(request.metadata), "recommended_path": path},
        )

    def _next_request(self, request: LoopRequest, diagnosis: FailureDiagnosis) -> LoopRequest:
        """按诊断策略把重规划提示并入下一轮请求约束。"""
        hints = diagnosis.strategy.replan_hints
        if not self._config.apply_correction_hints or not hints:
            return request
        merged = tuple(dict.fromkeys((*request.acceptance_criteria, *hints)))
        return replace(request, acceptance_criteria=merged)

    async def _write_episode(
        self, goal: str, result: LoopResult, diagnosis: FailureDiagnosis | None
    ) -> None:
        """把一轮经验写入注入的经验写入器。"""
        if self._episodes is not None:
            await self._episodes.record(goal, result, diagnosis)


__all__ = [
    "EpisodeWriter",
    "LoopEngineeringConfig",
    "LoopEngineeringRuntime",
    "RoundRecord",
]
