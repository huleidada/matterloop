"""learning 协议与 matterloop-memory Episodic Memory 之间的桥接适配器。

learning 子包为了与 memory 包解耦，只依赖结构化的 ``EpisodeLike`` 与
``EpisodeSource`` 协议；本模块提供把 ``matterloop_memory`` 的
``EpisodicMemoryStore`` 接入这些协议的官方适配器，使工程闭环 Runtime
可以直接把经验写入并复用长期记忆。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from matterloop_core import LoopResult, LoopStatus
from matterloop_memory import EpisodeRecord, EpisodicMemoryStore

from matterloop_agents.analysis.models import FailureDiagnosis

_SUCCESS_TAG = "success"


@dataclass(frozen=True, slots=True)
class MemoryEpisodeView:
    """把 ``EpisodeRecord`` 投影成 learning 协议所需的最小只读结构。

    Args:
        goal: 该次执行的目标描述。
        succeeded: 该次执行是否以 ``COMPLETED`` 终态结束。
        failure_summary: 失败原因摘要；成功经验为空字符串。
        resolution: 解决方式或成功路径摘要；缺失时为空字符串。
        tags: 经验标签。
    """

    goal: str
    succeeded: bool
    failure_summary: str
    resolution: str
    tags: tuple[str, ...]


def episode_view(record: EpisodeRecord) -> MemoryEpisodeView:
    """把一条记忆包经验记录转换为 learning 协议视图。

    Args:
        record: memory 包中的经验记录。

    Returns:
        满足 ``EpisodeLike`` 协议的不可变视图。
    """
    return MemoryEpisodeView(
        goal=record.goal,
        succeeded=record.status is LoopStatus.COMPLETED,
        failure_summary=record.failure_summary or "",
        resolution=record.resolution or "",
        tags=record.tags,
    )


class EpisodicMemorySource:
    """把 ``EpisodicMemoryStore`` 适配成 learning 的 ``EpisodeSource`` 协议。

    Args:
        store: memory 包提供的 Episodic Memory 存储实现。
    """

    def __init__(self, store: EpisodicMemoryStore) -> None:
        self._store = store

    async def list_failures(self, limit: int) -> Sequence[MemoryEpisodeView]:
        """返回最多 ``limit`` 条失败经验视图。

        Args:
            limit: 返回结果数量上限。

        Returns:
            失败经验视图序列。
        """
        records = await self._store.list_failures(limit)
        return tuple(episode_view(record) for record in records)

    async def list_successes(self, limit: int) -> Sequence[MemoryEpisodeView]:
        """返回最多 ``limit`` 条成功经验视图。

        Args:
            limit: 返回结果数量上限。

        Returns:
            成功经验视图序列。
        """
        records = await self._store.list_successes(limit)
        return tuple(episode_view(record) for record in records)

    async def find_similar(self, goal: str, limit: int) -> Sequence[MemoryEpisodeView]:
        """按目标相似度检索经验并展开为协议视图。

        Args:
            goal: 当前任务目标。
            limit: 返回结果数量上限。

        Returns:
            按相似度降序排列的经验视图序列。
        """
        matches = await self._store.find_similar(goal, limit)
        return tuple(episode_view(match.record) for match in matches)


class EpisodicMemoryWriter:
    """把工程闭环的轮次经验写入 Episodic Memory。

    实现 ``LoopEngineeringRuntime`` 所需的 ``EpisodeWriter`` 协议：成功轮次
    以运行输出作为可复用的解决路径，失败轮次记录诊断摘要与归因标签。

    Args:
        store: memory 包提供的 Episodic Memory 存储实现。
    """

    def __init__(self, store: EpisodicMemoryStore) -> None:
        self._store = store

    async def record(
        self, goal: str, result: LoopResult, diagnosis: FailureDiagnosis | None
    ) -> None:
        """把一轮执行结果转换为经验记录并写入存储。

        Args:
            goal: 该轮执行的目标描述。
            result: 该轮 Loop 的终态结果。
            diagnosis: 该轮的失败诊断；完成轮次为 ``None``。
        """
        succeeded = result.status is LoopStatus.COMPLETED
        episode = EpisodeRecord(
            run_id=result.run_id,
            goal=goal,
            status=result.status,
            stop_reason=result.stop_reason,
            failure_summary=None if diagnosis is None else diagnosis.summary,
            resolution=result.output if succeeded and result.output else None,
            output_summary=result.output or None,
            tags=(_SUCCESS_TAG,) if diagnosis is None else (diagnosis.category.value,),
        )
        await self._store.record(episode)


__all__ = [
    "EpisodicMemorySource",
    "EpisodicMemoryWriter",
    "MemoryEpisodeView",
    "episode_view",
]
