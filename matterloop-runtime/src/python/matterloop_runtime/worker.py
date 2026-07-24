"""队列 Worker：租约驱动的并发消费、自动续租与优雅停机。"""

from __future__ import annotations

import asyncio
import math
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from uuid import uuid4

from matterloop_runtime.errors import QueueLeaseLostError
from matterloop_runtime.queueing import (
    QueueBackend,
    QueuedRun,
    QueueLease,
    RenewableQueueBackend,
)

CommandHandler = Callable[[QueuedRun], Awaitable[None]]
"""处理一条队列命令的异步回调；抛出异常表示处理失败。"""


@dataclass(slots=True)
class _LeaseHolder:
    """在处理与续租协程之间共享最新租约快照。"""

    lease: QueueLease


class QueueWorker:
    """从队列租用命令并发处理的工作进程核心。

    每个进程实例化一个 ``QueueWorker`` 并等待 :meth:`run_until_stopped`；多进程各自
    运行一个实例即可水平扩展 Worker 集群：队列租约保证同一命令同一时刻只被一个
    工作进程持有，进程崩溃后租约到期，命令会被其他进程重新租用。

    处理成功的命令会被确认（acknowledge），处理失败的命令会被释放（release）以便
    延迟重试；若队列后端实现了 ``renew``（见
    :class:`~matterloop_runtime.queueing.RenewableQueueBackend`），长任务处理期间会
    按固定间隔自动续租。

    Args:
        queue: 满足 ``QueueBackend`` 协议的队列后端。
        handler: 处理一条队列命令 payload 的异步回调。
        worker_id: 工作进程标识；缺省自动生成。
        max_concurrency: 同时处理的命令数量上限。
        lease_seconds: 每次租用与续租请求的租约秒数。
        renew_interval_seconds: 续租间隔秒数，应明显小于租约秒数。
        idle_poll_interval_seconds: 队列为空时的轮询间隔秒数。
        retry_delay_seconds: 处理失败后命令重新可用前的延迟秒数。
    """

    def __init__(
        self,
        queue: QueueBackend,
        handler: CommandHandler,
        *,
        worker_id: str | None = None,
        max_concurrency: int = 1,
        lease_seconds: float = 30.0,
        renew_interval_seconds: float = 10.0,
        idle_poll_interval_seconds: float = 0.05,
        retry_delay_seconds: float = 0.0,
    ) -> None:
        if max_concurrency < 1:
            raise ValueError("max_concurrency must be at least 1")
        _validate_positive_finite(lease_seconds, "lease_seconds")
        _validate_positive_finite(renew_interval_seconds, "renew_interval_seconds")
        _validate_positive_finite(idle_poll_interval_seconds, "idle_poll_interval_seconds")
        if not math.isfinite(retry_delay_seconds) or retry_delay_seconds < 0:
            raise ValueError("retry_delay_seconds must be finite and not negative")
        self._queue = queue
        self._handler = handler
        self._worker_id = worker_id or uuid4().hex
        self._lease_seconds = lease_seconds
        self._renew_interval_seconds = renew_interval_seconds
        self._idle_poll_interval_seconds = idle_poll_interval_seconds
        self._retry_delay_seconds = retry_delay_seconds
        self._semaphore = asyncio.Semaphore(max_concurrency)
        self._stop_requested = asyncio.Event()
        self._tasks: set[asyncio.Task[None]] = set()

    @property
    def worker_id(self) -> str:
        """返回当前工作进程标识。"""
        return self._worker_id

    def request_stop(self) -> None:
        """请求优雅停机：不再租用新命令，在途命令处理完后退出。"""
        self._stop_requested.set()

    async def run_until_stopped(self) -> None:
        """循环租用并并发处理命令，直到收到停机请求。

        并发度由信号量控制；队列为空时按轮询间隔休眠。收到 :meth:`request_stop`
        后停止租用新命令，并等待全部在途命令处理完成再返回。
        """
        try:
            while not self._stop_requested.is_set():
                await self._semaphore.acquire()
                if self._stop_requested.is_set():
                    self._semaphore.release()
                    break
                try:
                    lease = await self._queue.lease(self._worker_id, self._lease_seconds)
                except BaseException:
                    self._semaphore.release()
                    raise
                if lease is None:
                    self._semaphore.release()
                    await self._idle_wait()
                    continue
                task = asyncio.create_task(self._process(lease))
                self._tasks.add(task)
                task.add_done_callback(self._tasks.discard)
        finally:
            if self._tasks:
                await asyncio.gather(*tuple(self._tasks), return_exceptions=True)

    async def _idle_wait(self) -> None:
        """在空轮询间隔内休眠，同时保证停机请求可以立即打断。"""
        try:
            await asyncio.wait_for(
                self._stop_requested.wait(),
                timeout=self._idle_poll_interval_seconds,
            )
        except asyncio.TimeoutError:
            return

    async def _process(self, lease: QueueLease) -> None:
        """处理一条已租用的命令：成功确认、失败释放，期间按需续租。"""
        holder = _LeaseHolder(lease)
        renew_stop = asyncio.Event()
        renew_task: asyncio.Task[None] | None = None
        if isinstance(self._queue, RenewableQueueBackend):
            renew_task = asyncio.create_task(self._keep_renewed(self._queue, holder, renew_stop))
        try:
            try:
                await self._handler(lease.job)
            except Exception:
                await self._settle_renewal(renew_task, renew_stop)
                await self._queue.release(
                    holder.lease,
                    delay_seconds=self._retry_delay_seconds,
                )
                return
            await self._settle_renewal(renew_task, renew_stop)
            await self._queue.acknowledge(holder.lease)
        finally:
            if renew_task is not None and not renew_task.done():
                renew_stop.set()
                await renew_task
            self._semaphore.release()

    async def _keep_renewed(
        self,
        queue: RenewableQueueBackend,
        holder: _LeaseHolder,
        stop: asyncio.Event,
    ) -> None:
        """按固定间隔续租，直到处理结束或租约已经丢失。"""
        while True:
            try:
                await asyncio.wait_for(stop.wait(), timeout=self._renew_interval_seconds)
                return
            except asyncio.TimeoutError:
                pass
            try:
                holder.lease = await queue.renew(holder.lease, self._lease_seconds)
            except QueueLeaseLostError:
                return

    @staticmethod
    async def _settle_renewal(
        renew_task: asyncio.Task[None] | None,
        stop: asyncio.Event,
    ) -> None:
        """先停止续租协程，确保后续确认或释放使用最新租约快照。"""
        if renew_task is None:
            return
        stop.set()
        await renew_task


def _validate_positive_finite(value: float, field_name: str) -> None:
    """拒绝会破坏租约与轮询边界的非法秒数。"""
    if not math.isfinite(value) or value <= 0:
        raise ValueError(f"{field_name} must be a finite value greater than 0")
