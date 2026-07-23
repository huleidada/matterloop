"""从历史失败经验中聚合重复模式并给出修复建议。"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from matterloop_agents.learning._text import normalized_signature, overlap_score, tokenize
from matterloop_agents.learning.protocols import EpisodeLike, EpisodeSource

_DEFAULT_FIX = "暂无同类成功案例，建议人工分析该失败模式并沉淀处理策略"
_MAX_EXAMPLES = 3


@dataclass(frozen=True, slots=True)
class FailurePattern:
    """一类重复出现的失败模式。

    Args:
        signature: 归一化后的失败签名。
        occurrences: 该模式出现的次数。
        example_summaries: 代表性的原始失败摘要样例。
        suggested_fix: 建议的修复方式，优先取同类成功案例的 resolution。
    """

    signature: str
    occurrences: int
    example_summaries: tuple[str, ...]
    suggested_fix: str


class FailureLearningEngine:
    """按归一化签名聚合失败经验并识别重复模式。

    Args:
        source: 历史经验数据源。
    """

    def __init__(self, source: EpisodeSource) -> None:
        self._source = source

    async def summarize(self, limit: int = 50) -> Mapping[str, int]:
        """聚合失败原因并按归一化签名分组计数。

        Args:
            limit: 最多读取的失败经验条数。

        Returns:
            签名到出现次数的映射，按出现次数降序排列。
        """
        groups = self._group_failures(await self._source.list_failures(limit))
        counts = {signature: len(episodes) for signature, episodes in groups.items()}
        return dict(sorted(counts.items(), key=lambda item: item[1], reverse=True))

    async def patterns(
        self, min_occurrences: int = 2, limit: int = 50
    ) -> tuple[FailurePattern, ...]:
        """识别重复失败模式并给出修复建议。

        Args:
            min_occurrences: 判定为重复模式所需的最少出现次数。
            limit: 最多读取的失败与成功经验条数。

        Returns:
            按出现次数降序排列的失败模式元组。
        """
        groups = self._group_failures(await self._source.list_failures(limit))
        successes = list(await self._source.list_successes(limit))
        patterns = [
            FailurePattern(
                signature=signature,
                occurrences=len(episodes),
                example_summaries=tuple(
                    episode.failure_summary for episode in episodes[:_MAX_EXAMPLES]
                ),
                suggested_fix=self._suggest_fix(episodes, successes),
            )
            for signature, episodes in groups.items()
            if len(episodes) >= min_occurrences
        ]
        return tuple(sorted(patterns, key=lambda pattern: pattern.occurrences, reverse=True))

    @staticmethod
    def _group_failures(
        failures: Sequence[EpisodeLike],
    ) -> dict[str, list[EpisodeLike]]:
        """按归一化签名分组失败经验，忽略无法形成签名的条目。"""
        groups: dict[str, list[EpisodeLike]] = {}
        for episode in failures:
            signature = normalized_signature(episode.failure_summary)
            if not signature:
                continue
            groups.setdefault(signature, []).append(episode)
        return groups

    @staticmethod
    def _suggest_fix(episodes: Sequence[EpisodeLike], successes: Sequence[EpisodeLike]) -> str:
        """优先复用同类成功案例的 resolution 作为修复建议。"""
        group_tags = frozenset(tag for episode in episodes for tag in episode.tags)
        group_tokens = frozenset(token for episode in episodes for token in tokenize(episode.goal))
        best_fix = ""
        best_score = 0.0
        for success in successes:
            if not success.resolution.strip():
                continue
            # 标签命中的权重高于目标文本重叠，保证同领域案例优先。
            score = 2.0 * len(group_tags & frozenset(success.tags)) + overlap_score(
                group_tokens, tokenize(success.goal)
            )
            if score > best_score:
                best_score = score
                best_fix = success.resolution
        return best_fix if best_score > 0 else _DEFAULT_FIX


__all__ = ["FailureLearningEngine", "FailurePattern"]
