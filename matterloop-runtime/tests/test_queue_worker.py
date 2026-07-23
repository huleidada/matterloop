"""队列 Worker 消费、失败重试、续租与优雅停机测试。"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable

import pytest
from matterloop_core import LoopRequest
from matterloop_runtime import (
    InMemoryQueueBackend,
    QueueAction,
    QueuedRun,
    QueueWorker,
)


async def _wait_until(predicate: Callable[[], bool], timeout_seconds: float = 2.0) -> None:
    """轮询直到条件满足，超时则失败。"""

    async def poll() -> None:
        while not predicate():
            await asyncio.sleep(0.005)

    await asyncio.wait_for(poll(), timeout=timeout_seconds)


def _job(run_id: str) -> QueuedRun:
    return QueuedRun(run_id, QueueAction.START, request=LoopRequest("goal"))


async def _run_worker_until(
    worker: QueueWorker,
    condition: Callable[[], bool],
) -> None:
    """启动 Worker，等待条件满足后优雅停机。"""
    runner = asyncio.create_task(worker.run_until_stopped())
    try:
        await _wait_until(condition)
    finally:
        worker.request_stop()
        await asyncio.wait_for(runner, timeout=2.0)


async def test_worker_consumes_and_acknowledges_jobs() -> None:
    backend = InMemoryQueueBackend()
    for run_id in ("run-1", "run-2", "run-3"):
        await backend.enqueue(_job(run_id))
    handled: list[str] = []

    async def handler(job: QueuedRun) -> None:
        handled.append(job.run_id)

    worker = QueueWorker(backend, handler, max_concurrency=2, idle_poll_interval_seconds=0.01)
    await _run_worker_until(worker, lambda: len(handled) == 3)

    assert sorted(handled) == ["run-1", "run-2", "run-3"]
    assert await backend.lease("checker", 30) is None


async def test_worker_releases_failed_job_for_retry() -> None:
    backend = InMemoryQueueBackend()
    await backend.enqueue(_job("run-1"))
    attempts: list[int] = []

    async def handler(job: QueuedRun) -> None:
        attempts.append(1)
        if len(attempts) == 1:
            raise RuntimeError("transient failure")

    worker = QueueWorker(backend, handler, idle_poll_interval_seconds=0.01)
    await _run_worker_until(worker, lambda: len(attempts) == 2)

    assert len(attempts) == 2
    assert await backend.lease("checker", 30) is None


async def test_worker_graceful_stop_finishes_in_flight_job() -> None:
    backend = InMemoryQueueBackend()
    await backend.enqueue(_job("run-1"))
    started = asyncio.Event()
    proceed = asyncio.Event()
    finished: list[str] = []

    async def handler(job: QueuedRun) -> None:
        started.set()
        await proceed.wait()
        finished.append(job.run_id)

    worker = QueueWorker(backend, handler, idle_poll_interval_seconds=0.01)
    runner = asyncio.create_task(worker.run_until_stopped())
    await asyncio.wait_for(started.wait(), timeout=2.0)

    worker.request_stop()
    await asyncio.sleep(0.02)
    assert not runner.done()

    proceed.set()
    await asyncio.wait_for(runner, timeout=2.0)
    assert finished == ["run-1"]
    assert await backend.lease("checker", 30) is None


async def test_worker_renews_lease_for_long_running_job() -> None:
    backend = InMemoryQueueBackend()
    await backend.enqueue(_job("run-1"))
    handled: list[int] = []

    async def handler(job: QueuedRun) -> None:
        # 处理时长超过租约时长；不续租的话命令会被回收并重复投递。
        await asyncio.sleep(0.5)
        handled.append(1)

    worker = QueueWorker(
        backend,
        handler,
        lease_seconds=0.2,
        renew_interval_seconds=0.05,
        idle_poll_interval_seconds=0.01,
    )
    await _run_worker_until(worker, lambda: len(handled) == 1)

    assert len(handled) == 1
    assert await backend.lease("checker", 30) is None


async def test_worker_stops_promptly_when_queue_is_empty() -> None:
    backend = InMemoryQueueBackend()

    async def handler(job: QueuedRun) -> None:
        raise AssertionError("handler must not be called")

    worker = QueueWorker(backend, handler, idle_poll_interval_seconds=5.0)
    runner = asyncio.create_task(worker.run_until_stopped())
    await asyncio.sleep(0.05)

    worker.request_stop()
    await asyncio.wait_for(runner, timeout=1.0)


@pytest.mark.parametrize(
    ("field_name", "kwargs"),
    [
        ("max_concurrency", {"max_concurrency": 0}),
        ("lease_seconds", {"lease_seconds": 0.0}),
        ("renew_interval_seconds", {"renew_interval_seconds": -1.0}),
        ("idle_poll_interval_seconds", {"idle_poll_interval_seconds": 0.0}),
        ("retry_delay_seconds", {"retry_delay_seconds": -0.1}),
    ],
)
def test_worker_rejects_invalid_configuration(
    field_name: str,
    kwargs: dict[str, float],
) -> None:
    async def handler(job: QueuedRun) -> None:
        return None

    typed_handler: Callable[[QueuedRun], Awaitable[None]] = handler
    with pytest.raises(ValueError, match=field_name):
        QueueWorker(InMemoryQueueBackend(), typed_handler, **kwargs)  # type: ignore[arg-type]
