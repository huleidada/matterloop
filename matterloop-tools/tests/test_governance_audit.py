"""审计记录、摘要与内存落地测试。"""

import pytest
from matterloop_tools import AuditRecord, InMemoryAuditSink, Principal, stable_digest


def _record(
    record_id: str = "r1",
    principal: Principal | None = None,
    tool_name: str = "shell",
    decision: str = "allowed",
) -> AuditRecord:
    return AuditRecord(
        record_id,
        principal or Principal("agent-1", tenant_id="tenant-a"),
        tool_name,
        stable_digest({"command": "ls"}),
        decision,
        1.0,
        2.0,
    )


def test_stable_digest_is_deterministic_and_key_order_independent() -> None:
    first = stable_digest({"a": 1, "b": {"y": 2, "x": [1, 2]}})
    second = stable_digest({"b": {"x": [1, 2], "y": 2}, "a": 1})

    assert first == second
    assert len(first) == 64
    assert stable_digest({"a": 1}) != stable_digest({"a": 2})


def test_audit_record_validates_and_defaults() -> None:
    record = _record()

    assert record.result_digest is None
    assert record.arguments_snapshot is None
    assert record.error == ""

    with pytest.raises(ValueError):
        _record(record_id=" ")
    with pytest.raises(ValueError):
        _record(decision=" ")
    with pytest.raises(ValueError):
        AuditRecord("r1", Principal("agent-1"), "shell", "digest", "allowed", 2.0, 1.0)


def test_audit_record_freezes_optional_arguments_snapshot() -> None:
    record = AuditRecord(
        "r1",
        Principal("agent-1"),
        "shell",
        "digest",
        "allowed",
        1.0,
        2.0,
        arguments_snapshot={"command": "ls"},
    )

    assert record.arguments_snapshot is not None
    assert record.arguments_snapshot["command"] == "ls"
    with pytest.raises(TypeError):
        record.arguments_snapshot["command"] = "rm"  # type: ignore[index]


async def test_in_memory_sink_records_and_queries() -> None:
    sink = InMemoryAuditSink()
    principal_a = Principal("agent-1", tenant_id="tenant-a")
    principal_b = Principal("agent-2", tenant_id="tenant-b")

    await sink.record(_record("r1", principal_a, tool_name="shell"))
    await sink.record(_record("r2", principal_b, tool_name="http", decision="denied"))
    await sink.record(_record("r3", principal_a, tool_name="http"))

    assert tuple(record.record_id for record in sink.records()) == ("r1", "r2", "r3")
    assert tuple(record.record_id for record in sink.records_for_tool("http")) == ("r2", "r3")
    assert tuple(record.record_id for record in sink.records_for_principal(principal_a)) == (
        "r1",
        "r3",
    )
