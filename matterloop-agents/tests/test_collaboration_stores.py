"""协作仓储、邮箱、事件和制品内存实现测试。"""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from hashlib import sha256

import pytest
from matterloop_agents.collaboration.artifacts import ArtifactStore, InMemoryArtifactStore
from matterloop_agents.collaboration.errors import (
    ArtifactNotFoundError,
    TeamRunAlreadyExistsError,
    TeamRunNotFoundError,
    TeamStateConflictError,
)
from matterloop_agents.collaboration.events import (
    LocalTeamEventPublisher,
    TeamEvent,
    TeamEventType,
)
from matterloop_agents.collaboration.messages import AgentMessage, InMemoryMailbox, MessageType
from matterloop_agents.collaboration.models import (
    TaskSpec,
    TaskState,
    TeamRequest,
    TeamSnapshot,
    TeamStatus,
)
from matterloop_agents.collaboration.stores import InMemoryTeamRepository


def _snapshot(
    run_id: str,
    *,
    status: TeamStatus = TeamStatus.CREATED,
    version: int = 0,
    created_at: datetime | None = None,
) -> TeamSnapshot:
    """构造含一个冻结任务节点的团队快照。"""
    timestamp = created_at or datetime.now(timezone.utc)
    return TeamSnapshot(
        request=TeamRequest(goal="完成协作测试"),
        tasks=(
            TaskState(
                TaskSpec(
                    task_id=f"{run_id}-task",
                    description="实现指定任务",
                    capability="python",
                )
            ),
        ),
        run_id=run_id,
        status=status,
        version=version,
        created_at=timestamp,
        updated_at=timestamp,
    )


async def test_team_repository_create_load_and_explicit_failures() -> None:
    """创建和读取必须隔离对象，并为非法状态返回明确异常。"""
    repository = InMemoryTeamRepository()
    original = _snapshot("run-1")

    await repository.create(original)
    first = await repository.load("run-1")
    second = await repository.require("run-1")

    assert first == original
    assert first is not original
    assert first is not second
    assert first is not None
    assert first.tasks is not original.tasks
    with pytest.raises(TeamRunAlreadyExistsError):
        await repository.create(original)
    with pytest.raises(ValueError, match="version"):
        await repository.create(replace(original, run_id="run-2", version=1))
    assert await repository.load("missing") is None
    with pytest.raises(TeamRunNotFoundError):
        await repository.require("missing")
    with pytest.raises(TeamRunNotFoundError):
        await repository.save(_snapshot("missing"), expected_version=0)


async def test_team_repository_save_increments_version_and_preserves_creation_time() -> None:
    """成功的 CAS 保存必须返回新版本并保留最初创建时间。"""
    repository = InMemoryTeamRepository()
    original = _snapshot("run-1")
    await repository.create(original)

    candidate = replace(original, status=TeamStatus.RUNNING)
    saved = await repository.save(candidate, expected_version=0)
    loaded = await repository.require("run-1")

    assert saved.version == 1
    assert saved.status is TeamStatus.RUNNING
    assert saved.created_at == original.created_at
    assert saved.updated_at >= original.updated_at
    assert loaded == saved
    assert loaded is not saved
    with pytest.raises(ValueError, match="snapshot version"):
        await repository.save(replace(saved, version=0), expected_version=1)
    with pytest.raises(ValueError, match="negative"):
        await repository.save(saved, expected_version=-1)


async def test_team_repository_rejects_one_of_two_concurrent_cas_updates() -> None:
    """针对同一版本的并发写入必须只允许一个调用提交。"""
    repository = InMemoryTeamRepository()
    original = _snapshot("run-1")
    await repository.create(original)
    candidates = (
        replace(original, status=TeamStatus.PLANNING),
        replace(original, status=TeamStatus.RUNNING),
    )

    results = await asyncio.gather(
        *(repository.save(candidate, expected_version=0) for candidate in candidates),
        return_exceptions=True,
    )

    successes = [result for result in results if isinstance(result, TeamSnapshot)]
    conflicts = [result for result in results if isinstance(result, TeamStateConflictError)]
    assert len(successes) == 1
    assert len(conflicts) == 1
    assert successes[0].version == 1
    assert (await repository.require("run-1")).version == 1


async def test_team_repository_lists_stably_with_filter_and_pagination() -> None:
    """列表查询必须先稳定排序，再执行状态过滤和分页。"""
    repository = InMemoryTeamRepository()
    base_time = datetime(2026, 7, 14, tzinfo=timezone.utc)
    snapshots = (
        _snapshot("run-c", created_at=base_time + timedelta(seconds=1)),
        _snapshot("run-b", status=TeamStatus.RUNNING, created_at=base_time),
        _snapshot("run-a", created_at=base_time),
    )
    for snapshot in snapshots:
        await repository.create(snapshot)

    listed = await repository.list()
    paged = await repository.list(status=TeamStatus.CREATED, offset=1, limit=1)

    assert tuple(snapshot.run_id for snapshot in listed) == ("run-a", "run-b", "run-c")
    assert tuple(snapshot.run_id for snapshot in paged) == ("run-c",)
    with pytest.raises(ValueError, match="limit"):
        await repository.list(limit=0)
    with pytest.raises(ValueError, match="offset"):
        await repository.list(offset=-1)


async def test_team_repository_coordinates_cross_orchestrator_leases() -> None:
    """同一运行只能被一个控制器所有者持有，错误所有者不能释放。"""
    repository = InMemoryTeamRepository()
    await repository.create(_snapshot("run-1"))

    assert await repository.acquire_lease("run-1", "owner-a") is True
    assert await repository.acquire_lease("run-1", "owner-b") is False
    await repository.release_lease("run-1", "owner-b")
    assert await repository.acquire_lease("run-1", "owner-b") is False
    await repository.release_lease("run-1", "owner-a")
    assert await repository.acquire_lease("run-1", "owner-b") is True


async def test_nested_metadata_is_frozen_before_repository_storage() -> None:
    """调用方持有的嵌套容器不得绕过版本控制修改已保存快照。"""
    nested = {"labels": ["original"], "config": {"enabled": True}}
    snapshot = TeamSnapshot(
        request=TeamRequest("冻结元数据", metadata=nested),
        tasks=(),
        run_id="nested-metadata",
    )
    repository = InMemoryTeamRepository()
    await repository.create(snapshot)

    nested["labels"].append("mutated")
    nested["config"]["enabled"] = False
    loaded = await repository.require("nested-metadata")

    assert loaded.request.metadata["labels"] == ("original",)
    assert loaded.request.metadata["config"]["enabled"] is True


async def test_mailbox_keeps_fifo_order_across_filtered_receive() -> None:
    """按团队过滤领取不得打乱同一接收方的其余消息。"""
    mailbox = InMemoryMailbox()
    messages = (
        AgentMessage("team-a", "planner", "worker", MessageType.TASK_ASSIGNMENT, "一"),
        AgentMessage("team-b", "planner", "worker", MessageType.INFORMATION, "二"),
        AgentMessage("team-a", "reviewer", "worker", MessageType.FEEDBACK, "三"),
        AgentMessage("team-a", "planner", "other", MessageType.REQUEST, "四"),
    )
    for message in messages:
        await mailbox.send(message)

    peeked = await mailbox.peek("worker", team_run_id="team-a")
    first_team_message = await mailbox.receive("worker", team_run_id="team-a", limit=1)
    remaining = await mailbox.receive("worker")

    assert peeked == (messages[0], messages[2])
    assert first_team_message == (messages[0],)
    assert remaining == (messages[1], messages[2])
    assert await mailbox.pending_count("worker") == 0
    assert await mailbox.pending_count("other", team_run_id="team-a") == 1
    with pytest.raises(ValueError, match="already been sent"):
        await mailbox.send(messages[0])


async def test_local_event_publisher_supports_ordered_sync_and_async_handlers() -> None:
    """本地事件发布器必须按订阅顺序调用同步和异步处理器。"""
    publisher = LocalTeamEventPublisher()
    calls: list[str] = []

    def sync_handler(event: TeamEvent) -> None:
        assert event.event_type is TeamEventType.TEAM_STARTED
        calls.append("sync")

    async def async_handler(event: TeamEvent) -> None:
        await asyncio.sleep(0)
        assert event.snapshot.run_id == "run-1"
        calls.append("async")

    publisher.subscribe(sync_handler)
    publisher.subscribe(sync_handler)
    publisher.subscribe(async_handler)
    event = TeamEvent(TeamEventType.TEAM_STARTED, _snapshot("run-1"), metadata={"source": "test"})

    await publisher.publish(event)
    publisher.unsubscribe(sync_handler)
    await publisher.publish(event)

    assert calls == ["sync", "async", "async"]
    assert event.metadata == {"source": "test"}


async def test_artifact_store_uses_content_hash_and_explicit_missing_error() -> None:
    """内存制品存储必须返回可验证引用并支持幂等删除。"""
    store = InMemoryArtifactStore()
    assert isinstance(store, ArtifactStore)
    content = "MatterLoop 制品".encode()
    metadata = {"task_id": "task-1"}

    reference = await store.put(
        "team/a",
        "报告/final.txt",
        content,
        media_type="text/plain",
        metadata=metadata,
    )
    duplicate = await store.put("team/a", "报告/final.txt", content)
    metadata["task_id"] = "changed"

    assert reference.uri.startswith("artifact://team%2Fa/")
    assert reference.uri == duplicate.uri
    assert reference.metadata["sha256"] == sha256(content).hexdigest()
    assert reference.metadata["size_bytes"] == len(content)
    assert reference.metadata["task_id"] == "task-1"
    assert await store.read(reference) == content
    assert await store.read(reference.uri) == content
    assert await store.delete(reference) is True
    assert await store.delete(reference) is False
    with pytest.raises(ArtifactNotFoundError):
        await store.read(reference)
    with pytest.raises(ArtifactNotFoundError, match="unsupported"):
        await store.read("https://example.com/file")
