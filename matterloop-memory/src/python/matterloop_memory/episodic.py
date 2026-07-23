"""历史任务经验的 Episodic Memory 协议与内存实现。"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from typing import Protocol, runtime_checkable
from uuid import uuid4

from matterloop_core import LoopStatus, StopReason

_FAILURE_STATUSES: frozenset[LoopStatus] = frozenset(
    {
        LoopStatus.FAILED,
        LoopStatus.TIMED_OUT,
        LoopStatus.CANCELLED,
        LoopStatus.BLOCKED,
    }
)


@dataclass(frozen=True, slots=True)
class EpisodeRecord:
    """表示一次已结束运行的经验记录。"""

    run_id: str
    goal: str
    status: LoopStatus
    stop_reason: StopReason | None = None
    failure_summary: str | None = None
    resolution: str | None = None
    output_summary: str | None = None
    tags: tuple[str, ...] = ()
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    episode_id: str = field(default_factory=lambda: uuid4().hex)

    def __post_init__(self) -> None:
        """校验必填字段。"""
        if not self.run_id.strip():
            raise ValueError("run_id must not be empty")
        if not self.goal.strip():
            raise ValueError("goal must not be empty")


@dataclass(frozen=True, slots=True)
class EpisodeMatch:
    """保存经验检索结果与相似度评分。"""

    record: EpisodeRecord
    score: float


@runtime_checkable
class EpisodicMemoryStore(Protocol):
    """Episodic Memory 存储扩展协议。"""

    async def record(self, episode: EpisodeRecord) -> None:
        """新增或替换一条经验记录。"""
        ...

    async def attach_resolution(self, episode_id: str, resolution: str) -> EpisodeRecord:
        """为已有经验补充解决方案并返回更新后的记录。"""
        ...

    async def find_similar(self, goal: str, limit: int = 5) -> tuple[EpisodeMatch, ...]:
        """按目标相似度降序检索经验记录。"""
        ...

    async def list_failures(self, limit: int = 10) -> tuple[EpisodeRecord, ...]:
        """返回最近的失败经验记录。"""
        ...

    async def list_successes(self, limit: int = 10) -> tuple[EpisodeRecord, ...]:
        """返回最近的成功经验记录。"""
        ...


class InMemoryEpisodicMemory:
    """基于词项 Jaccard 相似度的并发安全经验记忆内存实现。"""

    def __init__(self, tokenizer: Callable[[str], Iterable[str]] | None = None) -> None:
        """初始化经验记忆。

        Args:
            tokenizer: 将文本切分为词项的可调用对象；省略时使用内置正则分词。
        """
        self._episodes: dict[str, EpisodeRecord] = {}
        self._tokenizer = tokenizer if tokenizer is not None else _default_tokenizer
        self._lock = asyncio.Lock()

    async def record(self, episode: EpisodeRecord) -> None:
        """新增或替换一条经验记录。

        Args:
            episode: 需要持久化的经验记录。
        """
        async with self._lock:
            self._episodes[episode.episode_id] = episode

    async def attach_resolution(self, episode_id: str, resolution: str) -> EpisodeRecord:
        """为已有经验补充解决方案。

        Args:
            episode_id: 经验记录标识。
            resolution: 解决该次问题的方案文本。

        Returns:
            合并解决方案之后的新记录。

        Raises:
            KeyError: 指定经验记录不存在。
            ValueError: resolution 为空。
        """
        if not resolution.strip():
            raise ValueError("resolution must not be empty")
        async with self._lock:
            episode = self._episodes.get(episode_id)
            if episode is None:
                raise KeyError(f"episode not found: {episode_id}")
            updated = replace(episode, resolution=resolution)
            self._episodes[episode_id] = updated
            return updated

    async def find_similar(self, goal: str, limit: int = 5) -> tuple[EpisodeMatch, ...]:
        """按目标相似度降序检索经验记录。

        Args:
            goal: 待匹配的任务目标文本。
            limit: 返回结果数量上限。

        Returns:
            相似度大于零的记录，按相似度降序、时间升序排序。

        Raises:
            ValueError: limit 小于一。
        """
        if limit < 1:
            raise ValueError("limit must be at least 1")
        query_terms = set(self._tokenizer(goal))
        async with self._lock:
            episodes = tuple(self._episodes.values())
        matches = [
            EpisodeMatch(episode, score)
            for episode in episodes
            if (score := _jaccard(query_terms, set(self._tokenizer(episode.goal)))) > 0
        ]
        matches.sort(
            key=lambda match: (-match.score, match.record.created_at, match.record.episode_id)
        )
        return tuple(matches[:limit])

    async def list_failures(self, limit: int = 10) -> tuple[EpisodeRecord, ...]:
        """返回最近的失败经验记录。

        Args:
            limit: 返回结果数量上限。

        Returns:
            状态属于失败类终态的记录，按时间降序排序。
        """
        return await self._list_by(lambda episode: episode.status in _FAILURE_STATUSES, limit)

    async def list_successes(self, limit: int = 10) -> tuple[EpisodeRecord, ...]:
        """返回最近的成功经验记录。

        Args:
            limit: 返回结果数量上限。

        Returns:
            状态为完成的记录，按时间降序排序。
        """
        return await self._list_by(lambda episode: episode.status is LoopStatus.COMPLETED, limit)

    async def _list_by(
        self, predicate: Callable[[EpisodeRecord], bool], limit: int
    ) -> tuple[EpisodeRecord, ...]:
        """按谓词过滤经验记录并按时间降序返回。"""
        if limit < 1:
            raise ValueError("limit must be at least 1")
        async with self._lock:
            selected = [episode for episode in self._episodes.values() if predicate(episode)]
        selected.sort(key=lambda episode: (episode.created_at, episode.episode_id), reverse=True)
        return tuple(selected[:limit])


def _default_tokenizer(text: str) -> frozenset[str]:
    """将文本切分为小写词项集合。"""
    return frozenset(re.findall(r"\w+", text.casefold()))


def _jaccard(left: set[str], right: set[str]) -> float:
    """计算两个词项集合的 Jaccard 相似度。"""
    if not left or not right:
        return 0.0
    return len(left & right) / len(left | right)
