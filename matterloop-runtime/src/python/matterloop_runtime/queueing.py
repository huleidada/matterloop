"""队列运行 DTO、协议及无外部依赖的内存实现。"""

from __future__ import annotations

import asyncio
import math
from collections.abc import Collection, Mapping
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from enum import Enum
from heapq import heapify, heappop, heappush
from typing import Protocol, runtime_checkable
from uuid import uuid4

from matterloop_core import LoopRequest, LoopResult, ResumeMode

from matterloop_runtime.errors import (
    DuplicateRunError,
    QueueLeaseLostError,
    RunNotFoundError,
    RunNotResumableError,
    RunRequestConflictError,
    RunUpdateConflictError,
)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


class QueueAction(str, Enum):
    """队列命令要求工作进程执行的动作。"""

    START = "start"
    RESUME = "resume"


class RunStatus(str, Enum):
    """队列视角下的一次运行状态。"""

    QUEUED = "queued"
    RUNNING = "running"
    PAUSED = "paused"
    BLOCKED = "blocked"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    TIMED_OUT = "timed_out"

    @property
    def is_settled(self) -> bool:
        """判断调用方是否可以停止等待当前结果。"""
        return self not in {self.QUEUED, self.RUNNING}

    @property
    def is_terminal(self) -> bool:
        """判断当前状态是否再也不能恢复或取消。"""
        return self in {self.COMPLETED, self.FAILED, self.CANCELLED, self.TIMED_OUT}


@dataclass(frozen=True, slots=True)
class QueuedRun:
    """跨队列传递的最小运行命令。"""

    run_id: str
    action: QueueAction
    request: LoopRequest | None = None
    resume_mode: ResumeMode = ResumeMode.CONTINUE
    enqueued_at: datetime = field(default_factory=_utc_now)

    def __post_init__(self) -> None:
        """确保启动和恢复命令不会携带歧义数据。"""
        if not self.run_id:
            raise ValueError("run_id must not be empty")
        if self.enqueued_at.tzinfo is None:
            raise ValueError("enqueued_at must include a timezone")
        if self.action is QueueAction.START and self.request is None:
            raise ValueError("START command requires a request")
        if self.action is QueueAction.RESUME and self.request is not None:
            raise ValueError("RESUME command must not contain a request")


@dataclass(frozen=True, slots=True)
class QueueLease:
    """工作进程对一条队列命令的限时所有权。"""

    lease_id: str
    job: QueuedRun
    worker_id: str
    expires_at: datetime
    attempt: int = 1

    def __post_init__(self) -> None:
        """校验租约能够被稳定识别和排序。"""
        if not self.lease_id or not self.worker_id:
            raise ValueError("lease_id and worker_id must not be empty")
        if self.expires_at.tzinfo is None:
            raise ValueError("expires_at must include a timezone")
        if self.attempt < 1:
            raise ValueError("attempt must be at least 1")


@dataclass(frozen=True, slots=True)
class RunRecord:
    """供 API 查询和 CAS 更新的不可变运行记录。"""

    run_id: str
    request: LoopRequest
    status: RunStatus = RunStatus.QUEUED
    version: int = 0
    result: LoopResult | None = None
    error: str = ""
    created_at: datetime = field(default_factory=_utc_now)
    updated_at: datetime = field(default_factory=_utc_now)

    def __post_init__(self) -> None:
        """校验记录标识、版本和时间信息。"""
        if not self.run_id:
            raise ValueError("run_id must not be empty")
        if self.version < 0:
            raise ValueError("version must not be negative")
        if self.result is not None and self.result.run_id != self.run_id:
            raise ValueError("result run_id must match record run_id")
        if self.created_at.tzinfo is None or self.updated_at.tzinfo is None:
            raise ValueError("record timestamps must include a timezone")


@runtime_checkable
class QueueProducer(Protocol):
    """Celery 等外部队列可实现的最小生产者协议。"""

    async def enqueue(self, job: QueuedRun) -> None:
        """提交一条运行命令。"""
        ...

    async def cancel(self, run_id: str) -> bool:
        """尽力取消尚未开始或正在执行的命令。"""
        ...


@runtime_checkable
class QueueBackend(QueueProducer, Protocol):
    """由 MatterLoop 工作进程主动拉取时使用的完整队列协议。"""

    async def lease(
        self,
        worker_id: str,
        lease_seconds: float | None = None,
    ) -> QueueLease | None:
        """租用一条可执行命令。"""
        ...

    async def acknowledge(self, lease: QueueLease) -> None:
        """确认命令已经处理完成。"""
        ...

    async def release(self, lease: QueueLease, *, delay_seconds: float = 0) -> None:
        """释放命令以便稍后重试。"""
        ...


@runtime_checkable
class RenewableQueueBackend(QueueBackend, Protocol):
    """允许长任务在租约到期前发送心跳的拉取队列协议。"""

    async def renew(self, lease: QueueLease, lease_seconds: float) -> QueueLease:
        """续期当前租约并返回新的租约快照。"""
        ...


@runtime_checkable
class RunRepository(Protocol):
    """保存运行查询状态并通过版本号提供 CAS 更新。"""

    async def create(self, record: RunRecord) -> None:
        """创建新运行记录。"""
        ...

    async def get(self, run_id: str) -> RunRecord | None:
        """读取运行记录。"""
        ...

    async def list(self, *, limit: int = 100, offset: int = 0) -> tuple[RunRecord, ...]:
        """按创建时间倒序列出运行记录。"""
        ...

    async def compare_and_set(
        self,
        run_id: str,
        expected_version: int,
        replacement: RunRecord,
    ) -> bool:
        """仅在版本匹配时原子替换运行记录。"""
        ...


@runtime_checkable
class RunEventReader(Protocol):
    """为集成层提供只读运行事件分页。"""

    async def list_events(
        self,
        run_id: str,
        *,
        after: str | None = None,
        limit: int = 100,
    ) -> tuple[Mapping[str, object], ...]:
        """读取指定运行的事件。"""
        ...


class InMemoryRunRepository:
    """适用于测试和单进程开发的并发安全运行仓储。"""

    def __init__(self) -> None:
        self._records: dict[str, RunRecord] = {}
        self._lock = asyncio.Lock()

    async def create(self, record: RunRecord) -> None:
        """创建记录；重复标识会失败。"""
        async with self._lock:
            if record.run_id in self._records:
                raise DuplicateRunError(record.run_id)
            self._records[record.run_id] = record

    async def get(self, run_id: str) -> RunRecord | None:
        """读取不可变记录。"""
        async with self._lock:
            return self._records.get(run_id)

    async def list(self, *, limit: int = 100, offset: int = 0) -> tuple[RunRecord, ...]:
        """按创建时间倒序分页。"""
        if limit < 1 or offset < 0:
            raise ValueError("limit must be positive and offset must not be negative")
        async with self._lock:
            records = sorted(self._records.values(), key=lambda item: item.created_at, reverse=True)
            return tuple(records[offset : offset + limit])

    async def compare_and_set(
        self,
        run_id: str,
        expected_version: int,
        replacement: RunRecord,
    ) -> bool:
        """执行进程内原子 CAS。"""
        if replacement.run_id != run_id:
            raise ValueError("replacement run_id must match target run_id")
        async with self._lock:
            current = self._records.get(run_id)
            if current is None or current.version != expected_version:
                return False
            if replacement.version != expected_version + 1:
                raise ValueError("replacement version must increment by one")
            self._records[run_id] = replacement
            return True


@dataclass(frozen=True, slots=True, order=True)
class _PendingJob:
    available_at: datetime
    sequence: int
    job: QueuedRun = field(compare=False)
    attempt: int = field(default=1, compare=False)


class InMemoryQueueBackend:
    """支持租约、重试延迟和取消的内存队列后端。"""

    def __init__(self) -> None:
        self._pending: list[_PendingJob] = []
        self._pending_sequence = 0
        self._leased: dict[str, QueueLease] = {}
        self._known_runs: set[str] = set()
        self._cancelled_runs: set[str] = set()
        self._lock = asyncio.Lock()

    async def enqueue(self, job: QueuedRun) -> None:
        """将命令加入可租用队列。"""
        async with self._lock:
            if job.run_id in self._known_runs:
                raise DuplicateRunError(job.run_id)
            self._known_runs.add(job.run_id)
            self._push_pending(job, _utc_now())

    async def lease(
        self,
        worker_id: str,
        lease_seconds: float | None = None,
    ) -> QueueLease | None:
        """租用第一条已到执行时间的命令。"""
        if not worker_id:
            raise ValueError("worker_id must not be empty")
        lease_seconds = 30.0 if lease_seconds is None else lease_seconds
        _validate_finite_seconds(lease_seconds, "lease_seconds", positive=True)
        async with self._lock:
            now = _utc_now()
            self._reclaim_expired(now)
            while self._pending:
                pending = self._pending[0]
                if pending.available_at > now:
                    return None
                heappop(self._pending)
                if pending.job.run_id in self._cancelled_runs:
                    self._known_runs.discard(pending.job.run_id)
                    continue
                lease = QueueLease(
                    lease_id=uuid4().hex,
                    job=pending.job,
                    worker_id=worker_id,
                    expires_at=now + timedelta(seconds=lease_seconds),
                    attempt=pending.attempt,
                )
                self._leased[lease.lease_id] = lease
                return lease
            return None

    async def acknowledge(self, lease: QueueLease) -> None:
        """确认租约并清理命令。"""
        async with self._lock:
            self._reclaim_expired(_utc_now())
            current = self._leased.get(lease.lease_id)
            if current != lease:
                return
            del self._leased[lease.lease_id]
            self._known_runs.discard(lease.job.run_id)
            self._cancelled_runs.discard(lease.job.run_id)

    async def release(self, lease: QueueLease, *, delay_seconds: float = 0) -> None:
        """释放有效租约并增加尝试次数。"""
        _validate_finite_seconds(delay_seconds, "delay_seconds", positive=False)
        async with self._lock:
            self._reclaim_expired(_utc_now())
            current = self._leased.get(lease.lease_id)
            if current != lease:
                return
            del self._leased[lease.lease_id]
            if lease.job.run_id in self._cancelled_runs:
                self._known_runs.discard(lease.job.run_id)
                return
            self._push_pending(
                lease.job,
                _utc_now() + timedelta(seconds=delay_seconds),
                attempt=lease.attempt + 1,
            )

    async def renew(self, lease: QueueLease, lease_seconds: float) -> QueueLease:
        """续期仍由调用方持有的租约。

        Args:
            lease: 上一次获得或续期返回的最新租约快照。
            lease_seconds: 从当前时刻开始计算的新租约秒数。

        Returns:
            到期时间已经更新的新租约快照。旧快照立即失效。

        Raises:
            ValueError: 租约秒数不是有限正数。
            QueueLeaseLostError: 租约已过期、已续期或已被回收。
        """
        _validate_finite_seconds(lease_seconds, "lease_seconds", positive=True)
        async with self._lock:
            now = _utc_now()
            self._reclaim_expired(now)
            current = self._leased.get(lease.lease_id)
            if current != lease:
                raise QueueLeaseLostError(lease.lease_id)
            renewed = replace(current, expires_at=now + timedelta(seconds=lease_seconds))
            self._leased[lease.lease_id] = renewed
            return renewed

    async def cancel(self, run_id: str) -> bool:
        """标记命令取消，并移除尚未租用的同标识命令。"""
        async with self._lock:
            if run_id not in self._known_runs:
                return False
            self._cancelled_runs.add(run_id)
            retained = [item for item in self._pending if item.job.run_id != run_id]
            removed = len(retained) != len(self._pending)
            if removed:
                heapify(retained)
                self._pending = retained
                self._known_runs.discard(run_id)
                self._cancelled_runs.discard(run_id)
            return True

    def _reclaim_expired(self, now: datetime) -> None:
        expired = [lease for lease in self._leased.values() if lease.expires_at <= now]
        for lease in expired:
            del self._leased[lease.lease_id]
            if lease.job.run_id in self._cancelled_runs:
                self._known_runs.discard(lease.job.run_id)
                self._cancelled_runs.discard(lease.job.run_id)
            else:
                self._push_pending(lease.job, now, attempt=lease.attempt + 1)

    def _push_pending(
        self,
        job: QueuedRun,
        available_at: datetime,
        *,
        attempt: int = 1,
    ) -> None:
        heappush(
            self._pending,
            _PendingJob(available_at, self._pending_sequence, job, attempt),
        )
        self._pending_sequence += 1


class QueueRuntime:
    """面向 API 和客户端的异步队列运行门面。"""

    def __init__(
        self,
        producer: QueueProducer,
        repository: RunRepository,
        *,
        event_reader: RunEventReader | None = None,
    ) -> None:
        self.producer = producer
        self.repository = repository
        self.event_reader = event_reader

    async def submit(self, request: LoopRequest, *, run_id: str | None = None) -> str:
        """创建运行记录并提交启动命令。

        Args:
            request: Loop 请求。
            run_id: 可选外部运行标识。

        Returns:
            可用于查询与取消的运行标识。
        """
        actual_run_id = run_id or uuid4().hex
        record = RunRecord(run_id=actual_run_id, request=request)
        try:
            await self.repository.create(record)
        except DuplicateRunError:
            existing = await self.repository.get(actual_run_id)
            if existing is not None and existing.request == request:
                return actual_run_id
            raise RunRequestConflictError(actual_run_id) from None
        try:
            await self.producer.enqueue(
                QueuedRun(actual_run_id, QueueAction.START, request=request)
            )
        except Exception as exc:
            await self._update(
                actual_run_id,
                status=RunStatus.FAILED,
                error=f"{type(exc).__name__}: {exc}",
                expected_statuses={RunStatus.QUEUED},
            )
            raise
        return actual_run_id

    async def get(self, run_id: str) -> RunRecord | None:
        """查询运行记录。"""
        return await self.repository.get(run_id)

    async def list(self, *, limit: int = 100, offset: int = 0) -> tuple[RunRecord, ...]:
        """分页列出运行记录。"""
        return await self.repository.list(limit=limit, offset=offset)

    async def result(self, run_id: str) -> LoopResult | None:
        """返回已经产生的 Loop 结果。"""
        record = await self.repository.get(run_id)
        return None if record is None else record.result

    async def wait(
        self,
        run_id: str,
        *,
        timeout_seconds: float | None = None,
        poll_interval_seconds: float = 0.1,
    ) -> RunRecord:
        """轮询直到运行进入可观察的稳定状态。

        Raises:
            RunNotFoundError: 运行记录不存在。
            TimeoutError: 等待超过调用方给定上限。
        """
        _validate_finite_seconds(
            poll_interval_seconds,
            "poll_interval_seconds",
            positive=True,
        )
        if timeout_seconds is not None:
            _validate_finite_seconds(timeout_seconds, "timeout_seconds", positive=False)
        deadline = (
            None if timeout_seconds is None else asyncio.get_running_loop().time() + timeout_seconds
        )
        while True:
            record = await self.repository.get(run_id)
            if record is None:
                raise RunNotFoundError(run_id)
            if record.status.is_settled:
                return record
            if deadline is not None and asyncio.get_running_loop().time() >= deadline:
                raise TimeoutError(f"timed out waiting for run: {run_id}")
            await asyncio.sleep(poll_interval_seconds)

    async def cancel(self, run_id: str) -> bool:
        """请求队列取消，并通过 CAS 更新查询状态。"""
        record = await self.repository.get(run_id)
        if record is None or record.status.is_terminal:
            return False
        accepted = record.status in {RunStatus.PAUSED, RunStatus.BLOCKED}
        if not accepted:
            accepted = await self.producer.cancel(run_id)
        if accepted:
            return await self._update(
                run_id,
                status=RunStatus.CANCELLED,
                expected_statuses={
                    RunStatus.QUEUED,
                    RunStatus.RUNNING,
                    RunStatus.PAUSED,
                    RunStatus.BLOCKED,
                },
            )
        return False

    async def resume(
        self,
        run_id: str,
        *,
        mode: ResumeMode = ResumeMode.CONTINUE,
    ) -> bool:
        """重新排队一次暂停或阻塞的运行。"""
        record = await self.repository.get(run_id)
        if record is None:
            raise RunNotFoundError(run_id)
        if record.status not in {RunStatus.PAUSED, RunStatus.BLOCKED}:
            raise RunNotResumableError(f"run is not resumable: {record.status.value}")
        updated = await self._update(
            run_id,
            status=RunStatus.QUEUED,
            result=None,
            error="",
            expected_statuses={RunStatus.PAUSED, RunStatus.BLOCKED},
        )
        if not updated:
            return False
        try:
            await self.producer.enqueue(QueuedRun(run_id, QueueAction.RESUME, resume_mode=mode))
        except Exception:
            await self._update(
                run_id,
                status=record.status,
                result=record.result,
                error=record.error,
                expected_statuses={RunStatus.QUEUED},
            )
            raise
        return True

    async def list_events(
        self,
        run_id: str,
        *,
        after: str | None = None,
        limit: int = 100,
    ) -> tuple[Mapping[str, object], ...]:
        """通过可选事件读取器返回运行审计事件。"""
        if self.event_reader is None:
            return ()
        return await self.event_reader.list_events(run_id, after=after, limit=limit)

    async def _update(
        self,
        run_id: str,
        *,
        status: RunStatus,
        result: LoopResult | None = None,
        error: str = "",
        expected_statuses: Collection[RunStatus] | None = None,
    ) -> bool:
        max_attempts = 16
        for _ in range(max_attempts):
            current = await self.repository.get(run_id)
            if current is None:
                raise RunNotFoundError(run_id)
            if current.status.is_terminal:
                return False
            if expected_statuses is not None and current.status not in expected_statuses:
                return False
            replacement = replace(
                current,
                status=status,
                version=current.version + 1,
                result=result,
                error=error,
                updated_at=_utc_now(),
            )
            if await self.repository.compare_and_set(run_id, current.version, replacement):
                return True
            await asyncio.sleep(0)
        raise RunUpdateConflictError(run_id, max_attempts)


def _validate_finite_seconds(value: float, field_name: str, *, positive: bool) -> None:
    """拒绝会破坏超时和租约边界的非有限秒数。"""
    if not math.isfinite(value):
        raise ValueError(f"{field_name} must be finite")
    if positive and value <= 0:
        raise ValueError(f"{field_name} must be greater than 0")
    if not positive and value < 0:
        raise ValueError(f"{field_name} must not be negative")
