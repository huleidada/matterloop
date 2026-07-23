"""组合策略、权限、配额与审计的 MCP 统一网关。"""

from __future__ import annotations

import threading
import time
from collections.abc import Awaitable, Callable, Mapping
from uuid import uuid4

from matterloop_tools.base import Tool, ToolContext, ToolResult
from matterloop_tools.governance.access import (
    AccessController,
    Principal,
    ToolAccessDeniedError,
)
from matterloop_tools.governance.audit import AuditRecord, AuditSink, stable_digest
from matterloop_tools.governance.policy import ToolAccessLevel, ToolPolicySet
from matterloop_tools.governance.quota import QuotaExceededError, QuotaTracker
from matterloop_tools.registry import ToolRegistry

ApprovalCallback = Callable[[Principal, str, Mapping[str, object]], Awaitable[bool]]
"""审批回调：返回 ``True`` 放行、``False`` 拒绝当前调用。"""

_DECISION_ALLOWED = "allowed"
_DECISION_DENIED = "denied"
_DECISION_QUOTA_EXCEEDED = "quota_exceeded"
_DECISION_ERROR = "error"


class McpGateway:
    """企业级 MCP 工具调用统一入口。

    在委托 ``ToolRegistry`` 执行之前依次完成风险分级、审批、访问控制与
    配额扣减，并保证无论成败每次调用都产生一条审计记录。实际执行仍复用
    注册表的参数快照与授权语义。

    Args:
        registry: 承担工具发现、授权与执行的现有注册表。
        policies: 工具风险分级策略集。
        access_controller: 主体维度的访问控制器。
        audit_sink: 审计记录落地实现。
        quota: 可选的配额记账器；``None`` 时跳过配额检查。
        approval_callback: ``APPROVAL_REQUIRED`` 工具的审批回调；未配置
            时该级别的调用一律拒绝并审计。
        capture_arguments: 是否在审计记录中保存参数原文快照；默认只保存
            确定性摘要以避免敏感内容泄露。
    """

    def __init__(
        self,
        registry: ToolRegistry,
        *,
        policies: ToolPolicySet,
        access_controller: AccessController,
        audit_sink: AuditSink,
        quota: QuotaTracker | None = None,
        approval_callback: ApprovalCallback | None = None,
        capture_arguments: bool = False,
    ) -> None:
        self._registry = registry
        self._policies = policies
        self._access_controller = access_controller
        self._audit_sink = audit_sink
        self._quota = quota
        self._approval_callback = approval_callback
        self._capture_arguments = capture_arguments
        self._run_lock = threading.Lock()
        self._run_calls: dict[tuple[str, str], int] = {}

    @staticmethod
    def principal_key(principal: Principal) -> str:
        """构造配额记账使用的主体键（``tenant:agent``）。

        Args:
            principal: 发起调用的主体身份。

        Returns:
            租户与 Agent 组合而成的稳定记账键；无租户时使用 ``-`` 占位。
        """
        return f"{principal.tenant_id or '-'}:{principal.agent_id}"

    async def invoke(
        self,
        principal: Principal,
        tool_name: str,
        arguments: Mapping[str, object],
        context: ToolContext,
    ) -> ToolResult:
        """治理并执行一次工具调用。

        流程依次为：风险分级与审批、访问控制、配额扣减、委托注册表执行；
        每个出口都会写入审计记录。

        Args:
            principal: 发起调用的主体身份。
            tool_name: 工具注册名称。
            arguments: 结构化工具参数。
            context: 运行和步骤上下文。

        Returns:
            注册表返回的标准工具结果。

        Raises:
            ToolAccessDeniedError: 审批缺失、审批拒绝或访问控制拒绝。
            QuotaExceededError: 主体配额或策略的单次运行调用上限超限。
        """
        record_id = uuid4().hex
        started_at = time.time()
        arguments_digest = stable_digest(arguments)

        async def audit(
            decision: str,
            *,
            result_digest: str | None = None,
            error: str = "",
        ) -> None:
            await self._audit_sink.record(
                AuditRecord(
                    record_id,
                    principal,
                    tool_name,
                    arguments_digest,
                    decision,
                    started_at,
                    time.time(),
                    result_digest=result_digest,
                    arguments_snapshot=dict(arguments) if self._capture_arguments else None,
                    error=error,
                )
            )

        # 1. 风险分级；高危工具必须先通过可注入的审批回调。
        policy = self._policies.classify(tool_name)
        if policy.access_level is ToolAccessLevel.APPROVAL_REQUIRED:
            if self._approval_callback is None:
                reason = "approval required but no approval callback configured"
                await audit(_DECISION_DENIED, error=reason)
                raise ToolAccessDeniedError(tool_name, reason)
            if not await self._approval_callback(principal, tool_name, arguments):
                reason = "approval callback rejected the call"
                await audit(_DECISION_DENIED, error=reason)
                raise ToolAccessDeniedError(tool_name, reason)

        # 2. 主体维度访问控制，默认拒绝语义由控制器实现。
        decision = await self._access_controller.authorize(principal, tool_name, arguments)
        if not decision.allowed:
            await audit(_DECISION_DENIED, error=decision.reason)
            raise ToolAccessDeniedError(tool_name, decision.reason)

        # 3. 配额：先检查策略声明的单次运行调用上限，再做主体配额扣减。
        if policy.max_calls_per_run is not None:
            exceeded = self._consume_run_call(context.run_id, tool_name, policy.max_calls_per_run)
            if exceeded is not None:
                await audit(_DECISION_QUOTA_EXCEEDED, error=str(exceeded))
                raise exceeded
        if self._quota is not None:
            try:
                self._quota.check_and_consume(self.principal_key(principal), tool=tool_name)
            except QuotaExceededError as exc:
                await audit(_DECISION_QUOTA_EXCEEDED, error=str(exc))
                raise

        # 4. 委托注册表执行，保持其参数快照与授权语义。
        try:
            result = await self._registry.invoke(tool_name, arguments, context=context)
        except Exception as exc:
            await audit(_DECISION_ERROR, error=str(exc))
            raise

        # 5. 成功路径记录结果摘要。
        await audit(_DECISION_ALLOWED, result_digest=stable_digest(result.content))
        return result

    async def register_tool(self, tool: Tool, *, replace: bool = False) -> None:
        """注册工具的薄代理，委托 ``ToolRegistry.register``。

        Args:
            tool: 需要注册的工具。
            replace: 是否替换同名工具。
        """
        await self._registry.register(tool, replace=replace)

    async def replace_tool(self, name: str, tool: Tool) -> None:
        """热替换工具的薄代理，委托 ``ToolRegistry.replace``。

        Args:
            name: 需要替换的注册名称。
            tool: 新工具实例。
        """
        await self._registry.replace(name, tool)

    async def remove_tool(self, name: str) -> None:
        """移除工具的薄代理，委托 ``ToolRegistry.unregister``。

        Args:
            name: 需要移除的注册名称。
        """
        await self._registry.unregister(name)

    def _consume_run_call(
        self,
        run_id: str,
        tool_name: str,
        max_calls_per_run: int,
    ) -> QuotaExceededError | None:
        """原子地消费策略声明的单次运行调用配额。

        Args:
            run_id: 当前 Loop 运行标识。
            tool_name: 工具注册名称。
            max_calls_per_run: 策略允许的最大调用次数。

        Returns:
            超限时返回待抛出的异常，否则记账成功并返回 ``None``。
        """
        run_key = (run_id, tool_name)
        with self._run_lock:
            used = self._run_calls.get(run_key, 0)
            if used >= max_calls_per_run:
                return QuotaExceededError(f"run:{run_id}", "calls", tool=tool_name)
            self._run_calls[run_key] = used + 1
        return None
