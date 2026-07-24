"""Episodic Memory 内存实现测试。"""

import asyncio

import pytest
from matterloop_core import LoopStatus, StopReason
from matterloop_memory import EpisodeRecord, InMemoryEpisodicMemory


def test_episodic_memory_ranks_matches_by_similarity() -> None:
    """find_similar 应按词项 Jaccard 相似度降序排序。"""

    async def scenario() -> None:
        memory = InMemoryEpisodicMemory()
        exact = EpisodeRecord("run-1", "optimize polymer simulation", LoopStatus.COMPLETED)
        partial = EpisodeRecord("run-2", "optimize database queries", LoopStatus.COMPLETED)
        unrelated = EpisodeRecord("run-3", "translate documents", LoopStatus.COMPLETED)
        await memory.record(exact)
        await memory.record(partial)
        await memory.record(unrelated)

        matches = await memory.find_similar("optimize polymer simulation", limit=10)

        assert [match.record.episode_id for match in matches[:2]] == [
            exact.episode_id,
            partial.episode_id,
        ]
        assert matches[0].score == 1.0
        assert matches[0].score > matches[1].score > 0
        assert all(match.record.episode_id != unrelated.episode_id for match in matches)

    asyncio.run(scenario())


def test_episodic_memory_supports_custom_tokenizer() -> None:
    """注入自定义 tokenizer 后相似度应基于其分词结果。"""

    async def scenario() -> None:
        memory = InMemoryEpisodicMemory(tokenizer=lambda text: text.split("/"))
        record = EpisodeRecord("run-1", "a/b/c", LoopStatus.COMPLETED)
        await memory.record(record)

        matches = await memory.find_similar("a/b/c")
        assert len(matches) == 1
        assert matches[0].score == 1.0

    asyncio.run(scenario())


def test_episodic_memory_attaches_resolution() -> None:
    """attach_resolution 应更新存储并返回新记录。"""

    async def scenario() -> None:
        memory = InMemoryEpisodicMemory()
        failure = EpisodeRecord(
            "run-1",
            "run lammps simulation",
            LoopStatus.FAILED,
            stop_reason=StopReason.COMPONENT_ERROR,
            failure_summary="力场文件缺失",
        )
        await memory.record(failure)

        updated = await memory.attach_resolution(failure.episode_id, "补充 GAFF2 力场文件")
        assert updated.resolution == "补充 GAFF2 力场文件"
        assert updated.failure_summary == "力场文件缺失"

        failures = await memory.list_failures()
        assert failures[0].resolution == "补充 GAFF2 力场文件"

        with pytest.raises(KeyError):
            await memory.attach_resolution("missing", "任意方案")
        with pytest.raises(ValueError):
            await memory.attach_resolution(failure.episode_id, "  ")

    asyncio.run(scenario())


def test_episodic_memory_lists_failures_and_successes() -> None:
    """失败与成功列表应按状态过滤且互不混入。"""

    async def scenario() -> None:
        memory = InMemoryEpisodicMemory()
        success = EpisodeRecord("run-1", "goal one", LoopStatus.COMPLETED)
        failed = EpisodeRecord("run-2", "goal two", LoopStatus.FAILED)
        timed_out = EpisodeRecord("run-3", "goal three", LoopStatus.TIMED_OUT)
        for record in (success, failed, timed_out):
            await memory.record(record)

        failures = await memory.list_failures()
        successes = await memory.list_successes()

        assert {record.episode_id for record in failures} == {
            failed.episode_id,
            timed_out.episode_id,
        }
        assert [record.episode_id for record in successes] == [success.episode_id]
        assert await memory.list_failures(limit=1) in ((failed,), (timed_out,))

    asyncio.run(scenario())


def test_episode_record_validates_required_fields() -> None:
    """经验记录必填字段应被校验。"""
    with pytest.raises(ValueError):
        EpisodeRecord(" ", "goal", LoopStatus.COMPLETED)
    with pytest.raises(ValueError):
        EpisodeRecord("run-1", " ", LoopStatus.COMPLETED)
