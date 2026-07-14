"""权限感知且支持安全热替换的工具注册表。"""

from __future__ import annotations

from collections.abc import Iterable, Mapping

from matterloop_runtime import (
    ComponentExistsError,
    ComponentNotFoundError,
    RuntimeContainer,
)

from matterloop_tools.base import (
    AllowAllToolAuthorizer,
    PermissionDecision,
    Tool,
    ToolAuthorizer,
    ToolContext,
    ToolResult,
)
from matterloop_tools.errors import (
    ToolNotFoundError,
    ToolPermissionDeniedError,
)


class ToolRegistry:
    """统一负责工具发现、授权、调用与生命周期。

    Args:
        tools: 无需异步启动的初始工具。
        authorizer: 每次调用前执行的授权器；默认显式放行。
    """

    def __init__(
        self,
        tools: Iterable[Tool] = (),
        *,
        authorizer: ToolAuthorizer | None = None,
    ) -> None:
        initial: dict[str, Tool] = {}
        for tool in tools:
            if tool.spec.name in initial:
                raise ComponentExistsError(tool.spec.name)
            initial[tool.spec.name] = tool
        self._components: RuntimeContainer[Tool] = RuntimeContainer(initial)
        self._authorizer = authorizer or AllowAllToolAuthorizer()

    async def register(self, tool: Tool, *, replace: bool = False) -> None:
        """注册工具，并在允许时安全替换同名实例。

        Args:
            tool: 需要注册的工具。
            replace: 是否替换同名工具。
        """
        await self._components.register(tool.spec.name, tool, replace=replace)

    async def replace(self, name: str, tool: Tool) -> None:
        """安全热替换一个工具。

        Args:
            name: 需要替换的注册名称。
            tool: 新工具实例，其规范名称必须与注册名称一致。
        """
        if tool.spec.name != name:
            raise ValueError("replacement tool name must match registry name")
        await self._components.replace(name, tool)

    async def unregister(self, name: str) -> None:
        """移除并安全关闭一个工具。"""
        try:
            await self._components.unregister(name)
        except ComponentNotFoundError as exc:
            raise ToolNotFoundError(name) from exc

    def get(self, name: str) -> Tool:
        """返回当前工具，主要用于读取稳定发现信息。"""
        try:
            return self._components.get(name)
        except ComponentNotFoundError as exc:
            raise ToolNotFoundError(name) from exc

    def names(self) -> tuple[str, ...]:
        """返回稳定排序的工具名称。"""
        return self._components.names()

    async def invoke(
        self,
        name: str,
        arguments: Mapping[str, object],
        *,
        context: ToolContext,
    ) -> ToolResult:
        """授权并调用执行期间固定的工具实例。

        Args:
            name: 工具注册名称。
            arguments: 结构化工具参数。
            context: 运行和步骤上下文。

        Returns:
            工具标准结果。

        Raises:
            ToolPermissionDeniedError: 授权器拒绝调用。
            ToolNotFoundError: 工具不存在。
        """
        decision = await self._authorizer.authorize(name, arguments, context)
        if decision is not PermissionDecision.ALLOW:
            raise ToolPermissionDeniedError(name)
        try:
            async with self._components.acquire(name) as tool:
                return await tool.invoke(arguments, context)
        except ComponentNotFoundError as exc:
            raise ToolNotFoundError(name) from exc

    async def aclose(self) -> None:
        """关闭注册表并释放所有空闲工具。"""
        await self._components.aclose()
