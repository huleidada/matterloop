"""为工具、Executor 与 Agent 提供非侵入式预算结构代理。"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from typing import Generic, Protocol, TypeVar

from matterloop_core import ExecutionResult, Executor, LoopContext, PlanStep
from matterloop_tools import Tool, ToolContext, ToolResult, ToolSpec

from matterloop_policies.usage import UsageAmount, UsageLedger

ContextT = TypeVar("ContextT")
ResultT = TypeVar("ResultT")
SpecT = TypeVar("SpecT")
EndpointContextT = TypeVar("EndpointContextT", contravariant=True)
EndpointResultT = TypeVar("EndpointResultT", covariant=True)
EndpointSpecT = TypeVar("EndpointSpecT", covariant=True)
ScopeResolver = Callable[[ContextT], str | Iterable[str]]


class _AgentEndpoint(Protocol[EndpointSpecT, EndpointContextT, EndpointResultT]):
    """预算代理所需的最小 Agent 端点结构。"""

    @property
    def spec(self) -> EndpointSpecT:
        """返回 Agent 发现信息。"""
        ...

    async def execute(self, context: EndpointContextT) -> EndpointResultT:
        """执行一个 Agent 任务。"""
        ...


class BudgetedTool:
    """在工具调用前强制 ``tool_calls`` 上限的结构代理。"""

    def __init__(
        self,
        tool: Tool,
        ledger: UsageLedger,
        *,
        scope_resolver: ScopeResolver[ToolContext] | None = None,
    ) -> None:
        self._tool = tool
        self._ledger = ledger
        self._scope_resolver = scope_resolver or _tool_scopes

    @property
    def spec(self) -> ToolSpec:
        """原样暴露被代理工具的发现信息。"""
        return self._tool.spec

    async def invoke(
        self,
        arguments: Mapping[str, object],
        context: ToolContext,
    ) -> ToolResult:
        """预留一次调用，异常或取消时回滚。"""
        reservation = self._ledger.reserve(self._scope_resolver(context), UsageAmount(tool_calls=1))
        try:
            result = await self._tool.invoke(arguments, context)
        except BaseException:
            self._ledger.rollback(reservation)
            raise
        self._ledger.commit(reservation)
        return result


class BudgetedExecutor:
    """在核心 Executor 调用前强制执行尝试次数上限。"""

    def __init__(
        self,
        executor: Executor,
        ledger: UsageLedger,
        *,
        scope_resolver: ScopeResolver[LoopContext] | None = None,
    ) -> None:
        self._executor = executor
        self._ledger = ledger
        self._scope_resolver = scope_resolver or _loop_scopes

    async def execute(self, step: PlanStep, context: LoopContext) -> ExecutionResult:
        """预留一次 Executor 尝试，异常或取消时回滚。"""
        reservation = self._ledger.reserve(self._scope_resolver(context), UsageAmount(attempts=1))
        try:
            result = await self._executor.execute(step, context)
        except BaseException:
            self._ledger.rollback(reservation)
            raise
        self._ledger.commit(reservation)
        return result


class BudgetedAgentEndpoint(Generic[SpecT, ContextT, ResultT]):
    """不依赖 agents 包的泛型 AgentEndpoint 预算代理。

    ``scope_resolver`` 可同时返回 team、child loop、task 和 agent scope。默认解析
    上下文的 ``team_run_id``；自定义 Agent 上下文应显式注入 resolver。
    """

    def __init__(
        self,
        endpoint: _AgentEndpoint[SpecT, ContextT, ResultT],
        ledger: UsageLedger,
        *,
        scope_resolver: ScopeResolver[ContextT] | None = None,
    ) -> None:
        self._endpoint = endpoint
        self._ledger = ledger
        self._scope_resolver = scope_resolver or _default_agent_scopes

    @property
    def spec(self) -> SpecT:
        """原样暴露被代理 Agent 的发现信息。"""
        return self._endpoint.spec

    async def execute(self, context: ContextT) -> ResultT:
        """预留一次 Agent 任务，异常或取消时回滚。"""
        reservation = self._ledger.reserve(
            self._scope_resolver(context), UsageAmount(agent_tasks=1)
        )
        try:
            result = await self._endpoint.execute(context)
        except BaseException:
            self._ledger.rollback(reservation)
            raise
        self._ledger.commit(reservation)
        return result


def _tool_scopes(context: ToolContext) -> tuple[str, ...]:
    return (context.run_id,)


def _loop_scopes(context: LoopContext) -> tuple[str, ...]:
    return (context.run_id,)


def _default_agent_scopes(context: object) -> tuple[str, ...]:
    run_id = getattr(context, "team_run_id", None)
    if not isinstance(run_id, str) or not run_id.strip():
        raise ValueError("agent context requires team_run_id or an explicit scope resolver")
    return (run_id,)


__all__ = ["BudgetedAgentEndpoint", "BudgetedExecutor", "BudgetedTool", "ScopeResolver"]
