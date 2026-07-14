"""队列协议、CAS 仓储与客户端门面测试。"""

from dataclasses import replace

import pytest
from matterloop_core import LoopRequest, ResumeMode
from matterloop_runtime import (
    InMemoryQueueBackend,
    InMemoryRunRepository,
    QueueAction,
    QueuedRun,
    QueueRuntime,
    RunNotResumableError,
    RunRecord,
    RunStatus,
)


async def test_queue_backend_lease_release_and_acknowledge() -> None:
    backend = InMemoryQueueBackend()
    request = LoopRequest("goal")
    await backend.enqueue(QueuedRun("run-1", QueueAction.START, request=request))

    lease = await backend.lease("worker", 30)
    assert lease is not None
    assert lease.attempt == 1
    await backend.release(lease)
    retried = await backend.lease("worker", 30)
    assert retried is not None
    assert retried.attempt == 2
    await backend.acknowledge(retried)
    assert await backend.lease("worker", 30) is None


async def test_repository_compare_and_set_rejects_stale_version() -> None:
    repository = InMemoryRunRepository()
    original = RunRecord("run-1", LoopRequest("goal"))
    await repository.create(original)
    replacement = replace(original, version=1, status=RunStatus.RUNNING)

    assert await repository.compare_and_set("run-1", 0, replacement)
    assert not await repository.compare_and_set("run-1", 0, replacement)


async def test_queue_runtime_submit_cancel_and_resume() -> None:
    backend = InMemoryQueueBackend()
    repository = InMemoryRunRepository()
    runtime = QueueRuntime(backend, repository)

    run_id = await runtime.submit(LoopRequest("goal"), run_id="run-1")
    assert run_id == "run-1"
    record = await runtime.get(run_id)
    assert record is not None and record.status is RunStatus.QUEUED
    assert await runtime.cancel(run_id)
    assert (await runtime.get(run_id)).status is RunStatus.CANCELLED  # type: ignore[union-attr]

    await runtime.submit(LoopRequest("resumable"), run_id="run-2")
    lease = await backend.lease("worker", 30)
    assert lease is not None
    await backend.acknowledge(lease)
    paused = await repository.get("run-2")
    assert paused is not None
    assert await repository.compare_and_set(
        "run-2",
        paused.version,
        replace(paused, status=RunStatus.PAUSED, version=paused.version + 1),
    )
    assert await runtime.resume("run-2", mode=ResumeMode.REPLAN)
    resumed = await backend.lease("worker", 30)
    assert resumed is not None
    assert resumed.job.action is QueueAction.RESUME
    assert resumed.job.resume_mode is ResumeMode.REPLAN


async def test_queue_runtime_rejects_non_resumable_record() -> None:
    runtime = QueueRuntime(InMemoryQueueBackend(), InMemoryRunRepository())
    await runtime.submit(LoopRequest("goal"), run_id="run-1")

    with pytest.raises(RunNotResumableError):
        await runtime.resume("run-1")
