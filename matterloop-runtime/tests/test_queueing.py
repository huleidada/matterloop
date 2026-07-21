"""队列协议、CAS 仓储与客户端门面测试。"""

import asyncio
import math
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import matterloop_runtime.queueing as queueing_module
import pytest
from matterloop_core import LoopRequest, ResumeMode
from matterloop_runtime import (
    InMemoryQueueBackend,
    InMemoryRunRepository,
    QueueAction,
    QueuedRun,
    QueueLeaseLostError,
    QueueRuntime,
    RunNotResumableError,
    RunRecord,
    RunRequestConflictError,
    RunStatus,
    RunUpdateConflictError,
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


async def test_queue_backend_leases_ready_job_before_delayed_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """延迟重试不应阻塞后来入队但已经可以执行的任务。"""
    now = [datetime(2026, 1, 1, tzinfo=timezone.utc)]
    monkeypatch.setattr(queueing_module, "_utc_now", lambda: now[0])
    backend = InMemoryQueueBackend()
    request = LoopRequest("goal")
    await backend.enqueue(QueuedRun("delayed", QueueAction.START, request=request))
    delayed = await backend.lease("worker", 30)
    assert delayed is not None
    await backend.release(delayed, delay_seconds=60)
    await backend.enqueue(QueuedRun("ready", QueueAction.START, request=request))

    ready = await backend.lease("worker", 30)
    assert ready is not None
    assert ready.job.run_id == "ready"
    assert await backend.lease("worker", 30) is None

    now[0] += timedelta(seconds=60)
    retried = await backend.lease("worker", 30)
    assert retried is not None
    assert retried.job.run_id == "delayed"
    assert retried.attempt == 2


async def test_queue_backend_preserves_fifo_for_equal_availability(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """相同可用时间的任务应按入队顺序稳定租用。"""
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    monkeypatch.setattr(queueing_module, "_utc_now", lambda: now)
    backend = InMemoryQueueBackend()
    request = LoopRequest("goal")
    for run_id in ("run-1", "run-2", "run-3"):
        await backend.enqueue(QueuedRun(run_id, QueueAction.START, request=request))

    leased_run_ids: list[str] = []
    for _ in range(3):
        lease = await backend.lease("worker", 30)
        assert lease is not None
        leased_run_ids.append(lease.job.run_id)

    assert leased_run_ids == ["run-1", "run-2", "run-3"]


async def test_queue_backend_renew_invalidates_old_lease_snapshot() -> None:
    backend = InMemoryQueueBackend()
    await backend.enqueue(QueuedRun("run-1", QueueAction.START, request=LoopRequest("goal")))
    original = await backend.lease("worker", 30)
    assert original is not None

    renewed = await backend.renew(original, 60)
    assert renewed.lease_id == original.lease_id
    assert renewed.expires_at > original.expires_at

    await backend.acknowledge(original)
    await backend.release(original)
    with pytest.raises(QueueLeaseLostError):
        await backend.renew(original, 60)

    await backend.acknowledge(renewed)
    assert await backend.lease("worker", 30) is None


@pytest.mark.parametrize("invalid", [math.nan, math.inf, -math.inf])
async def test_queue_backend_rejects_non_finite_timing_values(invalid: float) -> None:
    backend = InMemoryQueueBackend()
    request = LoopRequest("goal")
    await backend.enqueue(QueuedRun("run-1", QueueAction.START, request=request))

    with pytest.raises(ValueError, match="finite"):
        await backend.lease("worker", invalid)

    lease = await backend.lease("worker", 30)
    assert lease is not None
    with pytest.raises(ValueError, match="finite"):
        await backend.renew(lease, invalid)
    with pytest.raises(ValueError, match="finite"):
        await backend.release(lease, delay_seconds=invalid)


async def test_expired_lease_cannot_acknowledge_or_release_reclaimed_job() -> None:
    backend = InMemoryQueueBackend()
    await backend.enqueue(QueuedRun("run-1", QueueAction.START, request=LoopRequest("goal")))
    expired = await backend.lease("worker-1", 0.001)
    assert expired is not None
    await asyncio.sleep(0.01)

    await backend.acknowledge(expired)
    await backend.release(expired)
    recovered = await backend.lease("worker-2", 30)
    assert recovered is not None
    assert recovered.attempt == 2


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


async def test_queue_runtime_submit_is_idempotent_for_same_request() -> None:
    backend = InMemoryQueueBackend()
    repository = InMemoryRunRepository()
    runtime = QueueRuntime(backend, repository)
    request = LoopRequest("goal", metadata={"tenant_id": "tenant-1"})

    assert await runtime.submit(request, run_id="stable-run") == "stable-run"
    assert await runtime.submit(request, run_id="stable-run") == "stable-run"
    first = await backend.lease("worker", 30)
    assert first is not None
    assert await backend.lease("worker", 30) is None


async def test_queue_runtime_submit_rejects_same_id_for_different_request() -> None:
    runtime = QueueRuntime(InMemoryQueueBackend(), InMemoryRunRepository())
    await runtime.submit(LoopRequest("first"), run_id="stable-run")

    with pytest.raises(RunRequestConflictError):
        await runtime.submit(LoopRequest("second"), run_id="stable-run")


@pytest.mark.parametrize("invalid", [math.nan, math.inf, -math.inf])
async def test_queue_runtime_wait_rejects_non_finite_timing_values(invalid: float) -> None:
    runtime = QueueRuntime(InMemoryQueueBackend(), InMemoryRunRepository())
    await runtime.submit(LoopRequest("goal"), run_id="run-1")

    with pytest.raises(ValueError, match="finite"):
        await runtime.wait("run-1", timeout_seconds=invalid)
    with pytest.raises(ValueError, match="finite"):
        await runtime.wait("run-1", poll_interval_seconds=invalid)


class _AlwaysConflictingRepository(InMemoryRunRepository):
    async def compare_and_set(
        self,
        run_id: str,
        expected_version: int,
        replacement: RunRecord,
    ) -> bool:
        """模拟版本持续变化导致 CAS 永远失败。"""
        return False


async def test_queue_runtime_raises_when_cas_retry_budget_is_exhausted() -> None:
    backend = InMemoryQueueBackend()
    repository = _AlwaysConflictingRepository()
    runtime = QueueRuntime(backend, repository)
    await repository.create(RunRecord("run-1", LoopRequest("goal"), status=RunStatus.PAUSED))

    with pytest.raises(RunUpdateConflictError, match="after 16 attempts"):
        await runtime.resume("run-1")


class _TerminalWinningRepository(InMemoryRunRepository):
    def __init__(self) -> None:
        super().__init__()
        self._won = False

    async def compare_and_set(
        self,
        run_id: str,
        expected_version: int,
        replacement: RunRecord,
    ) -> bool:
        """第一次竞争时模拟 Worker 率先写入完成终态。"""
        if not self._won:
            self._won = True
            current = await self.get(run_id)
            assert current is not None
            completed = replace(
                current,
                status=RunStatus.COMPLETED,
                version=current.version + 1,
            )
            assert await super().compare_and_set(run_id, current.version, completed)
            return False
        return await super().compare_and_set(run_id, expected_version, replacement)


async def test_queue_runtime_does_not_overwrite_concurrent_terminal_state() -> None:
    backend = InMemoryQueueBackend()
    repository = _TerminalWinningRepository()
    runtime = QueueRuntime(backend, repository)
    await runtime.submit(LoopRequest("goal"), run_id="run-1")

    assert not await runtime.cancel("run-1")
    record = await runtime.get("run-1")
    assert record is not None
    assert record.status is RunStatus.COMPLETED


async def test_queue_runtime_rejects_non_resumable_record() -> None:
    runtime = QueueRuntime(InMemoryQueueBackend(), InMemoryRunRepository())
    await runtime.submit(LoopRequest("goal"), run_id="run-1")

    with pytest.raises(RunNotResumableError):
        await runtime.resume("run-1")
