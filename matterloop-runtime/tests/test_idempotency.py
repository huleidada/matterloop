"""幂等哈希与结果复用调用器测试。"""

from __future__ import annotations

from uuid import uuid4

import pytest
from matterloop_runtime import (
    DuplicateExecutionError,
    ExecutionRecord,
    ExecutionStatus,
    IdempotentInvoker,
    InMemoryExecutionLedger,
    canonical_request_hash,
)


def test_hash_is_stable_regardless_of_key_order() -> None:
    first = canonical_request_hash(
        "tool.call",
        {"b": 2, "a": {"y": [1, 2, {"k": "v"}], "x": "值"}},
    )
    second = canonical_request_hash(
        "tool.call",
        {"a": {"x": "值", "y": [1, 2, {"k": "v"}]}, "b": 2},
    )
    assert first == second
    assert len(first) == 64


def test_hash_distinguishes_operation_and_arguments() -> None:
    base = canonical_request_hash("tool.call", {"a": 1})
    assert canonical_request_hash("tool.other", {"a": 1}) != base
    assert canonical_request_hash("tool.call", {"a": 2}) != base
    assert canonical_request_hash("tool.call", {"a": [1, 2]}) != canonical_request_hash(
        "tool.call", {"a": [2, 1]}
    )


@pytest.mark.parametrize(
    "arguments",
    [
        {"value": object()},
        {"value": b"bytes"},
        {"value": {1: "non-str-key"}},
        {"value": float("nan")},
        {"value": {"nested": {"deep": object()}}},
    ],
)
def test_hash_rejects_non_serializable_arguments(arguments: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        canonical_request_hash("tool.call", arguments)


def test_hash_rejects_empty_operation() -> None:
    with pytest.raises(ValueError, match="operation"):
        canonical_request_hash("", {})


async def test_invoke_executes_once_and_reuses_committed_result() -> None:
    ledger = InMemoryExecutionLedger()
    invoker = IdempotentInvoker(ledger)
    calls: list[int] = []

    async def executor() -> str:
        calls.append(1)
        return "payload"

    first = await invoker.invoke(
        "tool.call",
        {"a": 1},
        executor,
        run_id="run-1",
        task_id="task-1",
        agent_id="agent-1",
        tool_id="tool-1",
    )
    second = await invoker.invoke("tool.call", {"a": 1}, executor, run_id="run-1")

    assert first == "payload"
    assert second == "payload"
    assert len(calls) == 1
    record = await ledger.find_by_hash(canonical_request_hash("tool.call", {"a": 1}))
    assert record is not None
    assert record.status is ExecutionStatus.COMMITTED
    assert record.task_id == "task-1"
    assert record.agent_id == "agent-1"
    assert record.tool_id == "tool-1"


@pytest.mark.parametrize(
    "existing_status",
    [ExecutionStatus.PREPARED, ExecutionStatus.EXECUTING],
)
async def test_invoke_raises_on_in_flight_duplicate(existing_status: ExecutionStatus) -> None:
    ledger = InMemoryExecutionLedger()
    invoker = IdempotentInvoker(ledger)
    request_hash = canonical_request_hash("tool.call", {"a": 1})
    existing = await ledger.prepare(
        ExecutionRecord(execution_id=uuid4().hex, run_id="run-1", request_hash=request_hash)
    )
    if existing_status is ExecutionStatus.EXECUTING:
        await ledger.mark_executing(existing.execution_id)

    async def executor() -> str:
        raise AssertionError("executor must not be called")

    with pytest.raises(DuplicateExecutionError) as excinfo:
        await invoker.invoke("tool.call", {"a": 1}, executor, run_id="run-1")
    assert excinfo.value.execution_id == existing.execution_id
    assert excinfo.value.status is existing_status


async def test_invoke_marks_failed_and_reraises_then_refuses_replay() -> None:
    ledger = InMemoryExecutionLedger()
    invoker = IdempotentInvoker(ledger)

    async def executor() -> str:
        raise RuntimeError("side effect exploded")

    with pytest.raises(RuntimeError, match="side effect exploded"):
        await invoker.invoke("tool.call", {"a": 1}, executor, run_id="run-1")

    record = await ledger.find_by_hash(canonical_request_hash("tool.call", {"a": 1}))
    assert record is not None
    assert record.status is ExecutionStatus.FAILED
    assert "RuntimeError: side effect exploded" in record.error

    with pytest.raises(DuplicateExecutionError):
        await invoker.invoke("tool.call", {"a": 1}, executor, run_id="run-1")


async def test_invoke_isolates_different_requests() -> None:
    ledger = InMemoryExecutionLedger()
    invoker = IdempotentInvoker(ledger)

    async def make_executor(value: str) -> str:
        return value

    async def executor_one() -> str:
        return await make_executor("one")

    async def executor_two() -> str:
        return await make_executor("two")

    assert await invoker.invoke("tool.call", {"a": 1}, executor_one, run_id="run-1") == "one"
    assert await invoker.invoke("tool.call", {"a": 2}, executor_two, run_id="run-1") == "two"
    assert len(await ledger.pending_records()) == 0
