"""支持 Session 生命周期与原子热替换的 MCP 服务注册表。"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager

from matterloop_runtime import ComponentNotFoundError, RuntimeClosedError, RuntimeContainer

from matterloop_tools.mcp.client import McpServerConnection
from matterloop_tools.mcp.errors import (
    McpCatalogStaleError,
    McpLifecycleError,
    McpServerNotFoundError,
    McpToolNameCollisionError,
)
from matterloop_tools.mcp.models import (
    McpCallResult,
    McpCatalog,
    McpPromptDefinition,
    McpPromptResult,
    McpResourceDefinition,
    McpResourceResult,
    McpResourceTemplateDefinition,
    McpToolDefinition,
)
from matterloop_tools.mcp.tool_adapter import McpToolAdapter


class McpServerRegistry:
    """管理多个 MCP 连接及其无中断热替换。

    注册时先启动新连接；只有初始化成功才对新调用可见。替换发生前已经取得旧连接租约的
    操作继续完成，最后一个旧调用退出后才根据连接所有权配置关闭旧 Session。
    """

    def __init__(self) -> None:
        self._connections: RuntimeContainer[McpServerConnection] = RuntimeContainer()
        self._active_operations = 0
        self._drained = asyncio.Event()
        self._drained.set()

    async def register(
        self,
        connection: McpServerConnection,
        *,
        replace: bool = False,
    ) -> None:
        """注册并启动 MCP 连接。

        Args:
            connection: 已注入 Session、配置和 Mapper 的连接。
            replace: 同名连接存在时是否原子替换。
        """
        try:
            await self._connections.register(
                connection.config.name,
                connection,
                replace=replace,
            )
        except RuntimeClosedError:
            raise McpLifecycleError("MCP server registry is closed") from None

    async def replace(self, server_name: str, connection: McpServerConnection) -> None:
        """安全热替换一个已注册 MCP 连接。

        Args:
            server_name: 当前连接注册名称。
            connection: 新连接，其配置名称必须一致。
        """
        if connection.config.name != server_name:
            raise ValueError("replacement MCP server name must match registry name")
        try:
            await self._connections.replace(server_name, connection)
        except ComponentNotFoundError as exc:
            raise McpServerNotFoundError(server_name) from exc
        except RuntimeClosedError:
            raise McpLifecycleError("MCP server registry is closed") from None

    async def unregister(self, server_name: str) -> None:
        """移除连接，并在旧操作完成后关闭受托管 Session。"""
        try:
            await self._connections.unregister(server_name)
        except ComponentNotFoundError as exc:
            raise McpServerNotFoundError(server_name) from exc
        except RuntimeClosedError:
            raise McpLifecycleError("MCP server registry is closed") from None

    def names(self) -> tuple[str, ...]:
        """返回稳定排序的 MCP 服务注册名称。"""
        return self._connections.names()

    async def list_tools(self, server_name: str) -> tuple[McpToolDefinition, ...]:
        """发现指定服务的全部 MCP 工具。"""
        async with self._acquire(server_name) as connection:
            return await connection.list_tools()

    async def discover_tools(self, server_name: str) -> tuple[McpToolAdapter, ...]:
        """发现并适配指定服务的全部 MatterLoop 工具。

        Raises:
            McpToolNameCollisionError: 两个远端名称映射为相同安全名称。
        """
        async with self._acquire(server_name) as connection:
            definitions = await connection.list_tools()
            namespace = connection.config.tool_namespace
            max_characters = connection.config.limits.max_result_characters
            max_content_blocks = connection.config.limits.max_content_blocks
            catalog_token = connection.catalog_token
        adapters = tuple(
            McpToolAdapter(
                self,
                server_name,
                namespace,
                definition,
                max_result_characters=max_characters,
                max_content_blocks=max_content_blocks,
                catalog_token=catalog_token,
            )
            for definition in definitions
        )
        names: set[str] = set()
        for adapter in adapters:
            if adapter.spec.name in names:
                raise McpToolNameCollisionError(adapter.spec.name)
            names.add(adapter.spec.name)
        return adapters

    async def call_tool(
        self,
        server_name: str,
        tool_name: str,
        arguments: Mapping[str, object],
        *,
        catalog_token: str | None = None,
    ) -> McpCallResult:
        """在一次固定连接租约内调用 MCP 工具，并校验可选目录令牌。"""
        async with self._acquire(server_name) as connection:
            if catalog_token is not None and catalog_token != connection.catalog_token:
                raise McpCatalogStaleError(server_name, tool_name)
            return await connection.call_tool(tool_name, arguments)

    async def list_resources(self, server_name: str) -> tuple[McpResourceDefinition, ...]:
        """发现指定服务的全部 MCP 资源。"""
        async with self._acquire(server_name) as connection:
            return await connection.list_resources()

    async def read_resource(self, server_name: str, uri: str) -> McpResourceResult:
        """在一次固定连接租约内读取 MCP 资源。"""
        async with self._acquire(server_name) as connection:
            return await connection.read_resource(uri)

    async def list_resource_templates(
        self,
        server_name: str,
    ) -> tuple[McpResourceTemplateDefinition, ...]:
        """发现指定服务的全部参数化 MCP 资源模板。"""
        async with self._acquire(server_name) as connection:
            return await connection.list_resource_templates()

    async def list_prompts(self, server_name: str) -> tuple[McpPromptDefinition, ...]:
        """发现指定服务的全部 MCP Prompt。"""
        async with self._acquire(server_name) as connection:
            return await connection.list_prompts()

    async def get_prompt(
        self,
        server_name: str,
        prompt_name: str,
        arguments: Mapping[str, object],
    ) -> McpPromptResult:
        """在一次固定连接租约内获取 MCP Prompt。"""
        async with self._acquire(server_name) as connection:
            return await connection.get_prompt(prompt_name, arguments)

    async def catalog(self, server_name: str) -> McpCatalog:
        """在一个连接租约内返回完整 MCP 能力目录。"""
        async with self._acquire(server_name) as connection:
            return await connection.catalog()

    async def aclose(self) -> None:
        """关闭注册表，并等待各连接租约自然结束后释放资源。"""
        await self._connections.aclose()
        await self._drained.wait()

    @asynccontextmanager
    async def _acquire(self, server_name: str) -> AsyncIterator[McpServerConnection]:
        acquired = False
        try:
            async with self._connections.acquire(server_name) as connection:
                self._active_operations += 1
                self._drained.clear()
                acquired = True
                yield connection
        except ComponentNotFoundError as exc:
            raise McpServerNotFoundError(server_name) from exc
        except RuntimeClosedError:
            raise McpLifecycleError("MCP server registry is closed") from None
        finally:
            # 必须等 RuntimeContainer 的租约退出和可能的连接关闭完成后再声明排空。
            if acquired:
                self._active_operations -= 1
                if self._active_operations == 0:
                    self._drained.set()
