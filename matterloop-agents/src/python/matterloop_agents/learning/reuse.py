"""历史经验召回与成功路径推荐。"""

from __future__ import annotations

from dataclasses import dataclass

from matterloop_agents.learning._text import overlap_score, tokenize
from matterloop_agents.learning.protocols import EpisodeLike, EpisodeSource

_RECOMMEND_CANDIDATES = 10


@dataclass(frozen=True, slots=True)
class ExperienceMatch:
    """一条召回的历史经验及其相似度评分。

    Args:
        episode: 召回的历史经验。
        score: 与查询目标的词项重叠相似度，取值 0 到 1。
    """

    episode: EpisodeLike
    score: float


class ExperienceReuse:
    """包装经验数据源，提供重排序召回与成功路径推荐。

    Args:
        source: 历史经验数据源。
    """

    def __init__(self, source: EpisodeSource) -> None:
        self._source = source

    async def recall(self, goal: str, limit: int = 5) -> tuple[ExperienceMatch, ...]:
        """召回与目标相似的经验并按词项重叠度重排序。

        Args:
            goal: 当前任务目标。
            limit: 最多返回的经验条数。

        Returns:
            按相似度降序排列的经验匹配元组。
        """
        candidates = await self._source.find_similar(goal, limit)
        goal_tokens = tokenize(goal)
        matches = [
            ExperienceMatch(
                episode=episode, score=overlap_score(goal_tokens, tokenize(episode.goal))
            )
            for episode in candidates
        ]
        matches.sort(key=lambda match: match.score, reverse=True)
        return tuple(matches[:limit])

    async def recommend_path(self, goal: str) -> str | None:
        """返回最相似成功经验的解决路径摘要。

        Args:
            goal: 当前任务目标。

        Returns:
            最相似且带有非空 resolution 的成功经验的解决路径；找不到时返回 ``None``。
        """
        matches = await self.recall(goal, limit=_RECOMMEND_CANDIDATES)
        for match in matches:
            if match.episode.succeeded and match.episode.resolution.strip():
                return match.episode.resolution
        return None


__all__ = ["ExperienceMatch", "ExperienceReuse"]
