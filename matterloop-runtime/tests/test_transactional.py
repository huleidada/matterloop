"""事务式三阶段执行器与崩溃恢复决策测试。"""

from __future__ import annotations

import pytest
from matterloop_runtime import (
    ExecutionLedgerError,
    ExecutionStatus,
    InMemoryExecutionLedger,
    ReconciliationOutcome,
    RecoveryAction,
    TransactionalExecutor,
)


async def test_three_phase_prepare_execute_commit() -> None:
    ledger = InMemoryExecutionLedger()
    executor = TransactionalExecutor(ledger)

    record = await executor.prepare("tool.call", {"a": 1}, run_id="run-1")
    assert record.status is ExecutionStatus.PREPARED

    async def side_effect() -> str:
        return "result"

    result = await executor.execute(record.execution_id, side_effect)
    assert result == "result"
    executing = await ledger.get(record.execution_id)
    assert executing is not None
    assert executing.status is ExecutionStatus.EXECUTING

    committed = await executor.commit(record.execution_id, result)
    assert committed.status is ExecutionStatus.COMMITTED
    assert committed.result_payload == "result"


async def test_prepare_returns_existing_record_for_same_request() -> None:
    ledger = InMemoryExecutionLedger()
    executor = TransactionalExecutor(ledger)

    first = await executor.prepare("tool.call", {"a": 1}, run_id="run-1")
    second = await executor.prepare("tool.call", {"a": 1}, run_id="run-1")
    assert first.execution_id == second.execution_id


async def test_execute_failure_marks_failed_and_reraises() -> None:
    ledger = InMemoryExecutionLedger()
    executor = TransactionalExecutor(ledger)
    record = await executor.prepare("tool.call", {"a": 1}, run_id="run-1")

    async def side_effect() -> str:
        raise ValueError("bad payload")

    with pytest.raises(ValueError, match="bad payload"):
        await executor.execute(record.execution_id, side_effect)
    failed = await ledger.get(record.execution_id)
    assert failed is not None
    assert failed.status is ExecutionStatus.FAILED
    assert "ValueError: bad payload" in failed.error


async def test_recover_releases_prepared_and_reconciles_executing() -> None:
    ledger = InMemoryExecutionLedger()
    executor = TransactionalExecutor(ledger)
    prepared = await executor.prepare("tool.call", {"a": 1}, run_id="run-1")
    executing = await executor.prepare("tool.call", {"a": 2}, run_id="run-1")
    other_run = await executor.prepare("tool.call", {"a": 3}, run_id="run-2")
    await ledger.mark_executing(executing.execution_id)

    decisions = {decision.execution_id: decision for decision in await executor.recover("run-1")}
    assert set(decisions) == {prepared.execution_id, executing.execution_id}
    assert decisions[prepared.execution_id].action is RecoveryAction.RELEASE
    assert decisions[executing.execution_id].action is RecoveryAction.RECONCILE
    assert decisions[executing.execution_id].reason

    prepared_after = await ledger.get(prepared.execution_id)
    assert prepared_after is not None
    assert prepared_after.status is ExecutionStatus.PREPARED
    executing_after = await ledger.get(executing.execution_id)
    assert executing_after is not None
    assert executing_after.status is ExecutionStatus.RECONCILIATION_REQUIRED
    untouched = await ledger.get(other_run.execution_id)
    assert untouched is not None
    assert untouched.status is ExecutionStatus.PREPARED


async def test_reconcile_committed_outcome_reuses_result() -> None:
    ledger = InMemoryExecutionLedger()
    executor = TransactionalExecutor(ledger)
    record = await executor.prepare("tool.call", {"a": 1}, run_id="run-1")
    await ledger.mark_executing(record.execution_id)
    await executor.recover("run-1")

    reconciled = await executor.reconcile(
        record.execution_id,
        outcome=ReconciliationOutcome.committed("verified result"),
    )
    assert reconciled.status is ExecutionStatus.COMMITTED
    assert reconciled.result_payload == "verified result"


async def test_reconcile_failed_outcome_records_error() -> None:
    ledger = InMemoryExecutionLedger()
    executor = TransactionalExecutor(ledger)
    record = await executor.prepare("tool.call", {"a": 1}, run_id="run-1")
    await ledger.mark_executing(record.execution_id)
    await executor.recover("run-1")

    reconciled = await executor.reconcile(
        record.execution_id,
        outcome=ReconciliationOutcome.failed("side effect never happened"),
    )
    assert reconciled.status is ExecutionStatus.FAILED
    assert reconciled.error == "side effect never happened"


async def test_reconcile_rejects_untracked_execution() -> None:
    executor = TransactionalExecutor(InMemoryExecutionLedger())

    with pytest.raises(ExecutionLedgerError, match="not found"):
        await executor.reconcile("missing", outcome=ReconciliationOutcome.failed("gone"))


def test_reconciliation_outcome_rejects_ambiguous_combinations() -> None:
    with pytest.raises(ValueError, match="result payload"):
        ReconciliationOutcome(is_committed=True)
    with pytest.raises(ValueError, match="error message"):
        ReconciliationOutcome(is_committed=False)
    with pytest.raises(ValueError, match="must not carry"):
        ReconciliationOutcome(is_committed=True, result_payload="x", error="y")
    with pytest.raises(ValueError, match="must not carry"):
        ReconciliationOutcome(is_committed=False, result_payload="x", error="y")
