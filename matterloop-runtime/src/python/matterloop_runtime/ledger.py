"""外部副作用的精确一次执行账本（Execution Ledger）。

账本以 ``request_hash`` 为幂等键记录每一次外部副作用的生命周期，状态机为
``PREPARED -> EXECUTING -> COMMITTED / FAILED``；崩溃恢复时无法证明副作用是否
发生的 ``EXECUTING`` 记录会被标记为 ``RECONCILIATION_REQUIRED``，等待宿主对账，
与 ``matterloop_core`` Loop 内核的恢复哲学保持一致：绝不盲目重放。
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from enum import Enum
from typing import Protocol, runtime_checkable

from matterloop_runtime.errors import RuntimeErrorBase


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


class ExecutionLedgerError(RuntimeErrorBase):
    """执行账本发生非法状态迁移或记录冲突。"""


class ExecutionStatus(str, Enum):
    """一次外部副作用执行在账本中的状态。"""

    PREPARED = "prepared"
    EXECUTING = "executing"
    COMMITTED = "committed"
    FAILED = "failed"
    RECONCILIATION_REQUIRED = "reconciliation_required"

    @property
    def is_pending(self) -> bool:
        """判断记录是否仍未到达可复用或已终结的状态。"""
        return self in {self.PREPARED, self.EXECUTING}


_LEGAL_TRANSITIONS: dict[ExecutionStatus, frozenset[ExecutionStatus]] = {
    ExecutionStatus.EXECUTING: frozenset({ExecutionStatus.PREPARED}),
    ExecutionStatus.COMMITTED: frozenset(
        {ExecutionStatus.EXECUTING, ExecutionStatus.RECONCILIATION_REQUIRED}
    ),
    ExecutionStatus.FAILED: frozenset(
        {ExecutionStatus.EXECUTING, ExecutionStatus.RECONCILIATION_REQUIRED}
    ),
    ExecutionStatus.RECONCILIATION_REQUIRED: frozenset({ExecutionStatus.EXECUTING}),
}


@dataclass(frozen=True, slots=True)
class ExecutionRecord:
    """一次外部副作用执行的不可变账本记录。"""

    execution_id: str
    run_id: str
    request_hash: str
    task_id: str | None = None
    agent_id: str | None = None
    tool_id: str | None = None
    status: ExecutionStatus = ExecutionStatus.PREPARED
    result_payload: str | None = None
    error: str = ""
    created_at: datetime = field(default_factory=_utc_now)
    updated_at: datetime = field(default_factory=_utc_now)
    attempt: int = 1

    def __post_init__(self) -> None:
        """校验记录标识、状态一致性和时间信息。"""
        if not self.execution_id or not self.run_id or not self.request_hash:
            raise ValueError("execution_id, run_id and request_hash must not be empty")
        if self.attempt < 1:
            raise ValueError("attempt must be at least 1")
        if self.created_at.tzinfo is None or self.updated_at.tzinfo is None:
            raise ValueError("record timestamps must include a timezone")
        if self.status is ExecutionStatus.COMMITTED and self.result_payload is None:
            raise ValueError("committed record requires a result payload")


@runtime_checkable
class ExecutionLedger(Protocol):
    """可替换的执行账本协议；实现必须保证状态迁移合法。"""

    async def prepare(self, record: ExecutionRecord) -> ExecutionRecord:
        """登记一条 ``PREPARED`` 记录；同 ``request_hash`` 已存在时返回已有记录。"""
        ...

    async def mark_executing(self, execution_id: str) -> ExecutionRecord:
        """将记录从 ``PREPARED`` 迁移到 ``EXECUTING``。"""
        ...

    async def commit(self, execution_id: str, result_payload: str) -> ExecutionRecord:
        """提交执行结果并迁移到 ``COMMITTED``。"""
        ...

    async def fail(self, execution_id: str, error: str) -> ExecutionRecord:
        """记录执行失败并迁移到 ``FAILED``。"""
        ...

    async def require_reconciliation(self, execution_id: str) -> ExecutionRecord:
        """将副作用不确定的 ``EXECUTING`` 记录标记为等待宿主对账。"""
        ...

    async def get(self, execution_id: str) -> ExecutionRecord | None:
        """按执行标识读取记录。"""
        ...

    async def find_by_hash(self, request_hash: str) -> ExecutionRecord | None:
        """按幂等键读取记录。"""
        ...

    async def pending_records(self, run_id: str | None = None) -> tuple[ExecutionRecord, ...]:
        """列出 ``PREPARED`` 或 ``EXECUTING`` 状态的记录。"""
        ...


class InMemoryExecutionLedger:
    """适用于测试和单进程开发的并发安全内存账本。"""

    def __init__(self) -> None:
        self._records: dict[str, ExecutionRecord] = {}
        self._by_hash: dict[str, str] = {}
        self._lock = asyncio.Lock()

    async def prepare(self, record: ExecutionRecord) -> ExecutionRecord:
        """幂等登记记录。

        Args:
            record: 状态必须为 ``PREPARED`` 的新记录。

        Returns:
            同 ``request_hash`` 已存在时返回已有记录（不新建）；否则返回落库后的记录。

        Raises:
            ExecutionLedgerError: 记录状态不是 ``PREPARED``，或执行标识与其他幂等键冲突。
        """
        async with self._lock:
            existing_id = self._by_hash.get(record.request_hash)
            if existing_id is not None:
                return self._records[existing_id]
            if record.status is not ExecutionStatus.PREPARED:
                raise ExecutionLedgerError(
                    f"prepare requires a PREPARED record: {record.execution_id}"
                )
            if record.execution_id in self._records:
                raise ExecutionLedgerError(
                    f"execution id is already bound to another request: {record.execution_id}"
                )
            self._records[record.execution_id] = record
            self._by_hash[record.request_hash] = record.execution_id
            return record

    async def mark_executing(self, execution_id: str) -> ExecutionRecord:
        """迁移到 ``EXECUTING``。

        Raises:
            ExecutionLedgerError: 记录不存在或当前状态不是 ``PREPARED``。
        """
        return await self._transition(execution_id, ExecutionStatus.EXECUTING)

    async def commit(self, execution_id: str, result_payload: str) -> ExecutionRecord:
        """迁移到 ``COMMITTED`` 并缓存结果。

        Raises:
            ExecutionLedgerError: 记录不存在或状态迁移非法。
        """
        return await self._transition(
            execution_id,
            ExecutionStatus.COMMITTED,
            result_payload=result_payload,
            error="",
        )

    async def fail(self, execution_id: str, error: str) -> ExecutionRecord:
        """迁移到 ``FAILED`` 并记录错误。

        Raises:
            ExecutionLedgerError: 记录不存在或状态迁移非法。
        """
        return await self._transition(execution_id, ExecutionStatus.FAILED, error=error)

    async def require_reconciliation(self, execution_id: str) -> ExecutionRecord:
        """迁移到 ``RECONCILIATION_REQUIRED``，等待宿主对账。

        Raises:
            ExecutionLedgerError: 记录不存在或当前状态不是 ``EXECUTING``。
        """
        return await self._transition(execution_id, ExecutionStatus.RECONCILIATION_REQUIRED)

    async def get(self, execution_id: str) -> ExecutionRecord | None:
        """读取不可变记录。"""
        async with self._lock:
            return self._records.get(execution_id)

    async def find_by_hash(self, request_hash: str) -> ExecutionRecord | None:
        """按幂等键读取记录。"""
        async with self._lock:
            execution_id = self._by_hash.get(request_hash)
            return None if execution_id is None else self._records[execution_id]

    async def pending_records(self, run_id: str | None = None) -> tuple[ExecutionRecord, ...]:
        """按创建时间升序列出待定记录。

        Args:
            run_id: 可选运行标识过滤条件。

        Returns:
            ``PREPARED`` 或 ``EXECUTING`` 状态的记录。
        """
        async with self._lock:
            pending = [
                record
                for record in self._records.values()
                if record.status.is_pending and (run_id is None or record.run_id == run_id)
            ]
            pending.sort(key=lambda record: (record.created_at, record.execution_id))
            return tuple(pending)

    async def _transition(
        self,
        execution_id: str,
        target: ExecutionStatus,
        *,
        result_payload: str | None = None,
        error: str | None = None,
    ) -> ExecutionRecord:
        """在锁内校验并执行一次状态迁移。"""
        async with self._lock:
            current = self._records.get(execution_id)
            if current is None:
                raise ExecutionLedgerError(f"execution not found: {execution_id}")
            if current.status not in _LEGAL_TRANSITIONS[target]:
                raise ExecutionLedgerError(
                    f"illegal transition {current.status.value} -> {target.value}: {execution_id}"
                )
            updated = replace(
                current,
                status=target,
                result_payload=(
                    current.result_payload if result_payload is None else result_payload
                ),
                error=current.error if error is None else error,
                updated_at=_utc_now(),
            )
            self._records[execution_id] = updated
            return updated
