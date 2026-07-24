"""MCP Gateway 端到端治理流程测试。"""

from collections.abc import Mapping

import pytest
from matterloop_tools import (
    AccessRule,
    InMemoryAuditSink,
    McpGateway,
    Principal,
    QuotaExceededError,
    QuotaLimits,
    QuotaTracker,
    RuleBasedAccessController,
    ToolAccessDeniedError,
    ToolAccessLevel,
    ToolContext,
    ToolPolicy,
    ToolPolicySet,
    ToolRegistry,
    ToolResult,
    ToolSpec,
    stable_digest,
)


class EchoTool:
    """返回构造标签的测试工具。"""

    def __init__(self, name: str, label: str) -> None:
        self.label = label
        self._spec = ToolSpec(name, "回显测试标签", {"type": "object"})

    @property
    def spec(self) -> ToolSpec:
        return self._spec

    async def invoke(
        self,
        arguments: Mapping[str, object],
        context: ToolContext,
    ) -> ToolResult:
        del arguments, context
        return ToolResult(self.label)


class FailingTool:
    """执行时抛出异常的测试工具。"""

    spec = ToolSpec("broken", "总是失败", {"type": "object"})

    async def invoke(
        self,
        arguments: Mapping[str, object],
        context: ToolContext,
    ) -> ToolResult:
        del arguments, context
        raise RuntimeError("tool exploded")


class RecordingApproval:
    """记录审批请求并返回预设结论的回调。"""

    def __init__(self, verdict: bool) -> None:
        self.verdict = verdict
        self.requests: list[tuple[Principal, str]] = []

    async def __call__(
        self,
        principal: Principal,
        tool_name: str,
        arguments: Mapping[str, object],
    ) -> bool:
        del arguments
        self.requests.append((principal, tool_name))
        return self.verdict


PRINCIPAL = Principal("agent-1", user_id="user-1", tenant_id="tenant-a", roles=("operator",))
ALLOW_ALL_RULES = (AccessRule(allowed_tools=("*",)),)


def _gateway(
    *,
    registry: ToolRegistry,
    sink: InMemoryAuditSink,
    policies: ToolPolicySet | None = None,
    rules: tuple[AccessRule, ...] = ALLOW_ALL_RULES,
    quota: QuotaTracker | None = None,
    approval_callback: RecordingApproval | None = None,
    capture_arguments: bool = False,
) -> McpGateway:
    return McpGateway(
        registry,
        policies=policies
        or ToolPolicySet([ToolPolicy("echo", ToolAccessLevel.WRITE, risk_score=10)]),
        access_controller=RuleBasedAccessController(rules),
        audit_sink=sink,
        quota=quota,
        approval_callback=approval_callback,
        capture_arguments=capture_arguments,
    )


async def test_gateway_invokes_tool_and_audits_success() -> None:
    sink = InMemoryAuditSink()
    gateway = _gateway(registry=ToolRegistry([EchoTool("echo", "ok")]), sink=sink)

    result = await gateway.invoke(PRINCIPAL, "echo", {"value": 1}, ToolContext("run"))

    assert result.content == "ok"
    records = sink.records()
    assert len(records) == 1
    record = records[0]
    assert record.decision == "allowed"
    assert record.principal == PRINCIPAL
    assert record.arguments_digest == stable_digest({"value": 1})
    assert record.result_digest == stable_digest("ok")
    assert record.arguments_snapshot is None
    assert record.error == ""
    assert record.finished_at >= record.started_at
    assert len(record.record_id) == 32


async def test_gateway_denies_and_audits_when_no_rule_matches() -> None:
    sink = InMemoryAuditSink()
    gateway = _gateway(registry=ToolRegistry([EchoTool("echo", "ok")]), sink=sink, rules=())

    with pytest.raises(ToolAccessDeniedError):
        await gateway.invoke(PRINCIPAL, "echo", {}, ToolContext("run"))

    record = sink.records()[0]
    assert record.decision == "denied"
    assert "no access rule" in record.error
    assert record.result_digest is None


async def test_gateway_denies_approval_required_without_callback() -> None:
    sink = InMemoryAuditSink()
    gateway = _gateway(
        registry=ToolRegistry([EchoTool("echo", "ok")]),
        sink=sink,
        policies=ToolPolicySet(),
    )

    with pytest.raises(ToolAccessDeniedError):
        await gateway.invoke(PRINCIPAL, "echo", {}, ToolContext("run"))

    record = sink.records()[0]
    assert record.decision == "denied"
    assert "no approval callback" in record.error


async def test_gateway_denies_when_approval_callback_rejects() -> None:
    sink = InMemoryAuditSink()
    approval = RecordingApproval(verdict=False)
    gateway = _gateway(
        registry=ToolRegistry([EchoTool("echo", "ok")]),
        sink=sink,
        policies=ToolPolicySet(),
        approval_callback=approval,
    )

    with pytest.raises(ToolAccessDeniedError):
        await gateway.invoke(PRINCIPAL, "echo", {}, ToolContext("run"))

    assert approval.requests == [(PRINCIPAL, "echo")]
    record = sink.records()[0]
    assert record.decision == "denied"
    assert "rejected" in record.error


async def test_gateway_executes_when_approval_callback_approves() -> None:
    sink = InMemoryAuditSink()
    approval = RecordingApproval(verdict=True)
    gateway = _gateway(
        registry=ToolRegistry([EchoTool("echo", "ok")]),
        sink=sink,
        policies=ToolPolicySet(),
        approval_callback=approval,
    )

    result = await gateway.invoke(PRINCIPAL, "echo", {}, ToolContext("run"))

    assert result.content == "ok"
    assert approval.requests == [(PRINCIPAL, "echo")]
    assert sink.records()[0].decision == "allowed"


async def test_gateway_audits_and_reraises_quota_exceeded() -> None:
    sink = InMemoryAuditSink()
    quota = QuotaTracker(default_limits=QuotaLimits(max_calls=1))
    gateway = _gateway(registry=ToolRegistry([EchoTool("echo", "ok")]), sink=sink, quota=quota)

    await gateway.invoke(PRINCIPAL, "echo", {}, ToolContext("run"))

    with pytest.raises(QuotaExceededError):
        await gateway.invoke(PRINCIPAL, "echo", {}, ToolContext("run"))

    # 记账键为 tenant:agent，超限调用不再落账。
    assert quota.usage("tenant-a:agent-1").calls == 1
    decisions = tuple(record.decision for record in sink.records())
    assert decisions == ("allowed", "quota_exceeded")
    assert "quota exceeded" in sink.records()[1].error


async def test_gateway_enforces_policy_max_calls_per_run() -> None:
    sink = InMemoryAuditSink()
    policies = ToolPolicySet(
        [ToolPolicy("echo", ToolAccessLevel.WRITE, risk_score=10, max_calls_per_run=1)]
    )
    gateway = _gateway(
        registry=ToolRegistry([EchoTool("echo", "ok")]),
        sink=sink,
        policies=policies,
    )

    await gateway.invoke(PRINCIPAL, "echo", {}, ToolContext("run-1"))

    with pytest.raises(QuotaExceededError):
        await gateway.invoke(PRINCIPAL, "echo", {}, ToolContext("run-1"))

    # 不同 run 的调用配额彼此独立。
    result = await gateway.invoke(PRINCIPAL, "echo", {}, ToolContext("run-2"))
    assert result.content == "ok"
    decisions = tuple(record.decision for record in sink.records())
    assert decisions == ("allowed", "quota_exceeded", "allowed")


async def test_gateway_audits_and_reraises_tool_execution_error() -> None:
    sink = InMemoryAuditSink()
    gateway = _gateway(
        registry=ToolRegistry([FailingTool()]),
        sink=sink,
        policies=ToolPolicySet([ToolPolicy("broken", ToolAccessLevel.WRITE, risk_score=10)]),
    )

    with pytest.raises(RuntimeError, match="tool exploded"):
        await gateway.invoke(PRINCIPAL, "broken", {}, ToolContext("run"))

    record = sink.records()[0]
    assert record.decision == "error"
    assert record.error == "tool exploded"
    assert record.result_digest is None


async def test_gateway_optionally_captures_arguments_snapshot() -> None:
    sink = InMemoryAuditSink()
    gateway = _gateway(
        registry=ToolRegistry([EchoTool("echo", "ok")]),
        sink=sink,
        capture_arguments=True,
    )

    await gateway.invoke(PRINCIPAL, "echo", {"secret": "value"}, ToolContext("run"))

    snapshot = sink.records()[0].arguments_snapshot
    assert snapshot is not None
    assert snapshot["secret"] == "value"


async def test_gateway_generates_unique_record_ids() -> None:
    sink = InMemoryAuditSink()
    gateway = _gateway(registry=ToolRegistry([EchoTool("echo", "ok")]), sink=sink)

    await gateway.invoke(PRINCIPAL, "echo", {}, ToolContext("run"))
    await gateway.invoke(PRINCIPAL, "echo", {}, ToolContext("run"))

    record_ids = {record.record_id for record in sink.records()}
    assert len(record_ids) == 2


async def test_gateway_tool_lifecycle_proxies_delegate_to_registry() -> None:
    registry = ToolRegistry()
    sink = InMemoryAuditSink()
    gateway = _gateway(registry=registry, sink=sink)

    await gateway.register_tool(EchoTool("echo", "v1"))
    assert registry.names() == ("echo",)

    await gateway.replace_tool("echo", EchoTool("echo", "v2"))
    result = await gateway.invoke(PRINCIPAL, "echo", {}, ToolContext("run"))
    assert result.content == "v2"

    await gateway.remove_tool("echo")
    assert registry.names() == ()
