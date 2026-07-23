"""执行账本状态机与幂等登记测试。"""

from __future__ import annotations

import pytest
from matterloop_runtime import (
    ExecutionLedgerError,
    ExecutionRecord,
    ExecutionStatus,
    InMemoryExecutionLedger,
)


def _record(
    execution_id: str = "exec-1",
    run_id: str = "run-1",
    request_hash: str = "hash-1",
) -> ExecutionRecord:
    return ExecutionRecord(execution_id=execution_id, run_id=run_id, request_hash=request_hash)


async def test_prepare_is_idempotent_by_request_hash() -> None:
    ledger = InMemoryExecutionLedger()
    first = await ledger.prepare(_record("exec-1"))
    second = await ledger.prepare(_record("exec-2"))

    assert first.execution_id == "exec-1"
    assert second.execution_id == "exec-1"
    assert await ledger.find_by_hash("hash-1") == first
    assert await ledger.get("exec-2") is None


async def test_prepare_rejects_non_prepared_record_and_duplicate_id() -> None:
    ledger = InMemoryExecutionLedger()
    await ledger.prepare(_record("exec-1", request_hash="hash-1"))

    with pytest.raises(ExecutionLedgerError, match="PREPARED"):
        await ledger.prepare(
            ExecutionRecord(
                execution_id="exec-2",
                run_id="run-1",
                request_hash="hash-2",
                status=ExecutionStatus.EXECUTING,
            )
        )
    with pytest.raises(ExecutionLedgerError, match="already bound"):
        await ledger.prepare(_record("exec-1", request_hash="hash-3"))


async def test_happy_path_transitions_and_payload() -> None:
    ledger = InMemoryExecutionLedger()
    prepared = await ledger.prepare(_record())
    assert prepared.status is ExecutionStatus.PREPARED

    executing = await ledger.mark_executing("exec-1")
    assert executing.status is ExecutionStatus.EXECUTING

    committed = await ledger.commit("exec-1", '{"answer": 42}')
    assert committed.status is ExecutionStatus.COMMITTED
    assert committed.result_payload == '{"answer": 42}'
    assert committed.updated_at >= prepared.updated_at


async def test_fail_records_error_message() -> None:
    ledger = InMemoryExecutionLedger()
    await ledger.prepare(_record())
    await ledger.mark_executing("exec-1")

    failed = await ledger.fail("exec-1", "boom")
    assert failed.status is ExecutionStatus.FAILED
    assert failed.error == "boom"


@pytest.mark.parametrize(
    "invalid_action",
    ["commit_from_prepared", "fail_from_prepared", "executing_twice", "commit_after_commit"],
)
async def test_illegal_transitions_raise(invalid_action: str) -> None:
    ledger = InMemoryExecutionLedger()
    await ledger.prepare(_record())

    if invalid_action == "commit_from_prepared":
        with pytest.raises(ExecutionLedgerError, match="illegal transition"):
            await ledger.commit("exec-1", "result")
    elif invalid_action == "fail_from_prepared":
        with pytest.raises(ExecutionLedgerError, match="illegal transition"):
            await ledger.fail("exec-1", "boom")
    elif invalid_action == "executing_twice":
        await ledger.mark_executing("exec-1")
        with pytest.raises(ExecutionLedgerError, match="illegal transition"):
            await ledger.mark_executing("exec-1")
    else:
        await ledger.mark_executing("exec-1")
        await ledger.commit("exec-1", "result")
        with pytest.raises(ExecutionLedgerError, match="illegal transition"):
            await ledger.commit("exec-1", "other")


async def test_transition_on_unknown_execution_raises() -> None:
    ledger = InMemoryExecutionLedger()

    with pytest.raises(ExecutionLedgerError, match="not found"):
        await ledger.mark_executing("missing")


async def test_require_reconciliation_only_from_executing() -> None:
    ledger = InMemoryExecutionLedger()
    await ledger.prepare(_record())

    with pytest.raises(ExecutionLedgerError, match="illegal transition"):
        await ledger.require_reconciliation("exec-1")

    await ledger.mark_executing("exec-1")
    marked = await ledger.require_reconciliation("exec-1")
    assert marked.status is ExecutionStatus.RECONCILIATION_REQUIRED

    committed = await ledger.commit("exec-1", "verified")
    assert committed.status is ExecutionStatus.COMMITTED


async def test_pending_records_filters_status_and_run_id() -> None:
    ledger = InMemoryExecutionLedger()
    await ledger.prepare(_record("exec-1", run_id="run-1", request_hash="hash-1"))
    await ledger.prepare(_record("exec-2", run_id="run-1", request_hash="hash-2"))
    await ledger.prepare(_record("exec-3", run_id="run-2", request_hash="hash-3"))
    await ledger.mark_executing("exec-2")
    await ledger.mark_executing("exec-3")
    await ledger.commit("exec-3", "done")

    all_pending = await ledger.pending_records()
    assert {record.execution_id for record in all_pending} == {"exec-1", "exec-2"}
    assert {record.status for record in all_pending} == {
        ExecutionStatus.PREPARED,
        ExecutionStatus.EXECUTING,
    }

    run_pending = await ledger.pending_records("run-2")
    assert run_pending == ()


def test_record_validation_rejects_bad_fields() -> None:
    with pytest.raises(ValueError, match="must not be empty"):
        ExecutionRecord(execution_id="", run_id="run-1", request_hash="hash-1")
    with pytest.raises(ValueError, match="attempt"):
        ExecutionRecord(execution_id="exec-1", run_id="run-1", request_hash="hash-1", attempt=0)
    with pytest.raises(ValueError, match="result payload"):
        ExecutionRecord(
            execution_id="exec-1",
            run_id="run-1",
            request_hash="hash-1",
            status=ExecutionStatus.COMMITTED,
        )
