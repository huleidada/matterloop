"""从历史观测中产出计划、工具与 Prompt 三类优化建议。"""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Protocol

from matterloop_agents.evaluation.metrics import TaskOutcome
from matterloop_agents.learning._text import tokenize
from matterloop_agents.learning.protocols import EpisodeSource


@dataclass(frozen=True, slots=True)
class StrategySuggestion:
    """一条可执行的策略优化建议。

    Args:
        kind: 建议类型，取值为 ``plan``、``tool`` 或 ``prompt``。
        description: 便于人类理解的建议描述。
        evidence: 支撑该建议的证据条目。
    """

    kind: str
    description: str
    evidence: tuple[str, ...]

    def __post_init__(self) -> None:
        """拒绝未定义的建议类型。"""
        if self.kind not in {"plan", "tool", "prompt"}:
            raise ValueError("kind must be one of 'plan', 'tool', 'prompt'")


class ToolStatsProvider(Protocol):
    """提供工具调用统计的只读接口。"""

    def tool_stats(self) -> Mapping[str, tuple[int, int]]:
        """返回工具名到 ``(成功次数, 失败次数)`` 的映射。"""
        ...


class StrategyOptimizer:
    """从历史任务观测或经验数据源中产出优化建议。

    Args:
        tool_stats: 可选的工具调用统计来源，用于工具选择优化。
        tool_success_threshold: 低于该成功率的工具会被建议替换。
        min_tool_calls: 参与工具评估所需的最少调用次数。
        min_word_occurrences: 判定失败反馈高频词所需的最少出现次数。
    """

    def __init__(
        self,
        *,
        tool_stats: ToolStatsProvider | None = None,
        tool_success_threshold: float = 0.5,
        min_tool_calls: int = 3,
        min_word_occurrences: int = 2,
    ) -> None:
        self._tool_stats = tool_stats
        self._tool_success_threshold = tool_success_threshold
        self._min_tool_calls = min_tool_calls
        self._min_word_occurrences = min_word_occurrences

    def suggest(self, outcomes: Sequence[TaskOutcome]) -> tuple[StrategySuggestion, ...]:
        """基于历史任务观测产出全部类型的优化建议。

        Args:
            outcomes: 历史任务执行观测序列。

        Returns:
            计划、工具与 Prompt 三类建议的合并元组。
        """
        suggestions: list[StrategySuggestion] = []
        suggestions.extend(self._plan_suggestions(outcomes))
        suggestions.extend(self._tool_suggestions())
        feedbacks = [
            record.verification.feedback
            for outcome in outcomes
            for record in outcome.result.records
            if not record.verification.passed and record.verification.feedback.strip()
        ]
        suggestions.extend(self._prompt_suggestions(feedbacks))
        return tuple(suggestions)

    async def suggest_from_episodes(
        self, source: EpisodeSource, limit: int = 50
    ) -> tuple[StrategySuggestion, ...]:
        """基于历史失败经验产出 Prompt 与工具优化建议。

        Args:
            source: 历史经验数据源。
            limit: 最多读取的失败经验条数。

        Returns:
            工具与 Prompt 类建议的合并元组。
        """
        failures = await source.list_failures(limit)
        summaries = [
            episode.failure_summary for episode in failures if episode.failure_summary.strip()
        ]
        return tuple((*self._tool_suggestions(), *self._prompt_suggestions(summaries)))

    def _plan_suggestions(self, outcomes: Sequence[TaskOutcome]) -> list[StrategySuggestion]:
        """把反复验证失败的步骤描述转化为计划优化建议。"""
        failure_counts: Counter[str] = Counter(
            record.step.description
            for outcome in outcomes
            for record in outcome.result.records
            if not record.verification.passed
        )
        return [
            StrategySuggestion(
                kind="plan",
                description=f"步骤 {description!r} 反复验证失败，建议拆分为更小步骤或调整执行顺序",
                evidence=(f"verification failed {count} times",),
            )
            for description, count in failure_counts.most_common()
            if count >= 2
        ]

    def _tool_suggestions(self) -> list[StrategySuggestion]:
        """把低成功率工具转化为工具选择优化建议。"""
        if self._tool_stats is None:
            return []
        suggestions: list[StrategySuggestion] = []
        for tool, (successes, failures) in sorted(self._tool_stats.tool_stats().items()):
            total = successes + failures
            if total < self._min_tool_calls:
                continue
            rate = successes / total
            if rate < self._tool_success_threshold:
                suggestions.append(
                    StrategySuggestion(
                        kind="tool",
                        description=f"工具 {tool!r} 成功率仅 {rate:.0%}，建议替换（switch_tool:{tool}）",
                        evidence=(f"successes={successes}, failures={failures}",),
                    )
                )
        return suggestions

    def _prompt_suggestions(self, failure_texts: Sequence[str]) -> list[StrategySuggestion]:
        """把失败反馈中的高频词转化为 Prompt 约束补充建议。"""
        word_counts: Counter[str] = Counter(
            token for text in failure_texts for token in tokenize(text) if len(token) >= 3
        )
        frequent = [
            (word, count)
            for word, count in word_counts.most_common(5)
            if count >= self._min_word_occurrences
        ]
        if not frequent:
            return []
        words = ", ".join(word for word, _ in frequent)
        return [
            StrategySuggestion(
                kind="prompt",
                description=f"失败反馈中高频出现关键词（{words}），建议在提示中补充对应约束",
                evidence=tuple(f"{word}: {count} 次" for word, count in frequent),
            )
        ]


__all__ = ["StrategyOptimizer", "StrategySuggestion", "ToolStatsProvider"]
