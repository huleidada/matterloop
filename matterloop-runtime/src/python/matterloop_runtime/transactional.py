"""事务式检查点执行器：Prepare/Execute/Commit 与崩溃恢复决策。

与 ``matterloop_core`` Loop 内核的恢复语义一致：崩溃后 ``PREPARED`` 记录尚未产生
副作用，可以安全重新发起；``EXECUTING`` 记录的副作用不确定，只标记为等待对账，
绝不自动重放，由宿主通过 :meth:`TransactionalExecutor.reconcile` 给出最终结局。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum
from uuid import uuid4

from matterloop_runtime.idempotency import ExecutionCallable, canonical_request_hash
from matterloop_runtime.ledger import (
    ExecutionLedger,
    ExecutionRecord,
    ExecutionStatus,
)


class RecoveryAction(str, Enum):
    """崩溃恢复时对一条待定记录的处置决策。"""

    RELEASE = "release"
    RECONCILE = "reconcile"


@dataclass(frozen=True, slots=True)
class RecoveryDecision:
    """针对一条待定执行记录的恢复决策。"""

    execution_id: str
    action: RecoveryAction
    reason: str


@dataclass(frozen=True, slots=True)
class ReconciliationOutcome:
    """宿主对账后给出的最终结局。

    使用 :meth:`committed` 或 :meth:`failed` 构造，避免歧义组合。
    """

    is_committed: bool
    result_payload: str | None = None
    error: str = ""

    def __post_init__(self) -> None:
        """校验结局字段组合的一致性。"""
        if self.is_committed:
            if self.result_payload is None:
                raise ValueError("committed outcome requires a result payload")
            if self.error:
                raise ValueError("committed outcome must not carry an error")
        else:
            if not self.error:
                raise ValueError("failed outcome requires an error message")
            if self.result_payload is not None:
                raise ValueError("failed outcome must not carry a result payload")

    @classmethod
    def committed(cls, result: str) -> ReconciliationOutcome:
        """构造副作用确认已发生的结局。

        Args:
            result: 对账确认的执行结果。

        Returns:
            已提交结局。
        """
        return cls(is_committed=True, result_payload=result)

    @classmethod
    def failed(cls, error: str) -> ReconciliationOutcome:
        """构造副作用确认未发生或已失败的结局。

        Args:
            error: 对账得出的失败原因。

        Returns:
            已失败结局。
        """
        return cls(is_committed=False, error=error)


class TransactionalExecutor:
    """显式三阶段（Prepare/Execute/Commit）事务检查点执行器。

    与 :class:`~matterloop_runtime.idempotency.IdempotentInvoker` 的一体化调用不同，
    本执行器把三个阶段拆开，供需要在外部系统之间插入自定义步骤（如先写业务库、
    再提交账本）的宿主编排；内部同样走账本状态机保证迁移合法。

    Args:
        ledger: 保存执行记录的账本。
    """

    def __init__(self, ledger: ExecutionLedger) -> None:
        self._ledger = ledger

    async def prepare(
        self,
        operation: str,
        arguments: Mapping[str, object],
        *,
        run_id: str,
        task_id: str | None = None,
        agent_id: str | None = None,
        tool_id: str | None = None,
    ) -> ExecutionRecord:
        """登记一条 ``PREPARED`` 记录；同幂等键已存在时返回已有记录。

        Args:
            operation: 操作名称，参与幂等键计算。
            arguments: 请求参数，参与幂等键计算。
            run_id: 所属运行标识。
            task_id: 可选任务标识。
            agent_id: 可选智能体标识。
            tool_id: 可选工具标识。

        Returns:
            新建或已存在的执行记录。

        Raises:
            ValueError: 请求参数无法确定性序列化。
        """
        request_hash = canonical_request_hash(operation, arguments)
        candidate = ExecutionRecord(
            execution_id=uuid4().hex,
            run_id=run_id,
            request_hash=request_hash,
            task_id=task_id,
            agent_id=agent_id,
            tool_id=tool_id,
        )
        return await self._ledger.prepare(candidate)

    async def execute(self, execution_id: str, executor: ExecutionCallable) -> str:
        """将记录迁移到 ``EXECUTING`` 并运行执行器；结果由宿主显式提交。

        Args:
            execution_id: 已 ``prepare`` 的执行标识。
            executor: 真正执行副作用并返回结果字符串的异步回调。

        Returns:
            执行器返回的结果，尚未提交到账本。

        Raises:
            ExecutionLedgerError: 记录不存在或状态迁移非法。
            Exception: 执行器抛出的原始异常；记录会先被标记为 ``FAILED``。
        """
        await self._ledger.mark_executing(execution_id)
        try:
            return await executor()
        except Exception as exc:
            await self._ledger.fail(execution_id, f"{type(exc).__name__}: {exc}")
            raise

    async def commit(self, execution_id: str, result: str) -> ExecutionRecord:
        """提交执行结果并迁移到 ``COMMITTED``。

        Args:
            execution_id: 已执行完成的执行标识。
            result: 需要缓存以供复用的结果。

        Returns:
            更新后的记录。

        Raises:
            ExecutionLedgerError: 记录不存在或状态迁移非法。
        """
        return await self._ledger.commit(execution_id, result)

    async def recover(self, run_id: str | None = None) -> tuple[RecoveryDecision, ...]:
        """扫描待定记录并给出崩溃恢复决策。

        ``PREPARED`` 记录尚未开始执行，决策为 ``RELEASE``（可安全重新发起）；
        ``EXECUTING`` 记录副作用不确定，账本状态被标记为
        ``RECONCILIATION_REQUIRED``，决策为 ``RECONCILE``，等待宿主对账，绝不自动重放。

        Args:
            run_id: 可选运行标识过滤条件。

        Returns:
            针对每条待定记录的决策。
        """
        decisions: list[RecoveryDecision] = []
        for record in await self._ledger.pending_records(run_id):
            if record.status is ExecutionStatus.PREPARED:
                decisions.append(
                    RecoveryDecision(
                        execution_id=record.execution_id,
                        action=RecoveryAction.RELEASE,
                        reason="execution never started; safe to re-initiate",
                    )
                )
            else:
                await self._ledger.require_reconciliation(record.execution_id)
                decisions.append(
                    RecoveryDecision(
                        execution_id=record.execution_id,
                        action=RecoveryAction.RECONCILE,
                        reason="side effect is uncertain; host reconciliation required",
                    )
                )
        return tuple(decisions)

    async def reconcile(
        self,
        execution_id: str,
        *,
        outcome: ReconciliationOutcome,
    ) -> ExecutionRecord:
        """宿主对账入口：写入外部系统核实后的最终结局。

        Args:
            execution_id: 等待对账的执行标识。
            outcome: :meth:`ReconciliationOutcome.committed` 或
                :meth:`ReconciliationOutcome.failed` 构造的结局。

        Returns:
            更新后的记录。

        Raises:
            ExecutionLedgerError: 记录不存在或状态迁移非法。
        """
        if outcome.is_committed:
            if outcome.result_payload is None:  # pragma: no cover - 构造器保证不变量
                raise ValueError("committed outcome requires a result payload")
            return await self._ledger.commit(execution_id, outcome.result_payload)
        return await self._ledger.fail(execution_id, outcome.error)
