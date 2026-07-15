"""带超时、分页和生命周期边界的 MCP 连接。"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping, Sequence
from contextlib import asynccontextmanager
from typing import TypeGuard, TypeVar
from uuid import uuid4

from matterloop_tools.mcp.errors import (
    McpCapabilityNotSupportedError,
    McpError,
    McpLifecycleError,
    McpPaginationLimitError,
    McpResponseLimitError,
    McpTimeoutError,
    McpTransportError,
)
from matterloop_tools.mcp.mapper import StructuralMcpResponseMapper
from matterloop_tools.mcp.models import (
    McpCallResult,
    McpCatalog,
    McpPromptDefinition,
    McpPromptPage,
    McpPromptResult,
    McpResourceDefinition,
    McpResourcePage,
    McpResourceResult,
    McpResourceTemplateDefinition,
    McpResourceTemplatePage,
    McpServerCapabilities,
    McpServerConfig,
    McpToolDefinition,
    McpToolPage,
)
from matterloop_tools.mcp.protocols import McpResponseMapper, McpSessionAdapter

PageT = TypeVar("PageT")
ItemT = TypeVar("ItemT")
_MISSING = object()


class McpServerConnection:
    """封装一个由调用方创建并注入的 MCP Session。

    Args:
        session: 已配置传输、端点和凭据的 Session Adapter。
        config: 生命周期、命名空间与本地硬限制。
        mapper: SDK 原始响应映射器；默认支持 Mapping 和属性对象。

    Notes:
        本类不读取环境变量，不创建网络客户端。仅当 ``owns_session=True`` 时关闭注入
        Session；是否同时关闭底层 transport 由 Session Adapter 自己决定。
    """

    def __init__(
        self,
        session: McpSessionAdapter,
        config: McpServerConfig,
        *,
        mapper: McpResponseMapper | None = None,
    ) -> None:
        self._session = session
        self._config = config
        self._mapper = mapper or StructuralMcpResponseMapper()
        self._lifecycle_lock = asyncio.Lock()
        self._operation_lock = asyncio.Lock()
        self._operations_drained = asyncio.Event()
        self._operations_drained.set()
        self._active_operations = 0
        self._capabilities = McpServerCapabilities()
        self._catalog_token = uuid4().hex
        self._started = False
        self._closed = False

    @property
    def config(self) -> McpServerConfig:
        """返回不可变连接配置。"""
        return self._config

    @property
    def capabilities(self) -> McpServerCapabilities:
        """返回初始化阶段协商得到的服务端能力快照。"""
        return self._capabilities

    @property
    def catalog_token(self) -> str:
        """返回绑定当前连接目录与工具适配器的不透明令牌。"""
        return self._catalog_token

    async def start(self) -> None:
        """初始化连接并完成可选 MCP 能力协商。

        Raises:
            McpLifecycleError: 连接已关闭。
            McpTimeoutError: 初始化超过配置时限。
            McpTransportError: Session 初始化失败。
        """
        async with self._lifecycle_lock:
            if self._closed:
                raise McpLifecycleError("MCP connection is closed")
            if self._started:
                return
            if self._config.initialize_on_start:
                initialization = await self._execute(
                    "initialize",
                    self._session.initialize(),
                    self._config.limits.initialize_timeout_seconds,
                )
                self._capabilities = self._mapper.map_capabilities(initialization)
            self._started = True

    async def list_tools(self) -> tuple[McpToolDefinition, ...]:
        """读取全部工具，并强制执行分页与条目上限。"""
        async with self._operation():
            self._ensure_capability("tools", self._capabilities.tools)
            return await self._paginate(
                "list_tools",
                self._list_tool_page,
                lambda page: page.items,
                lambda page: page.next_cursor,
            )

    async def call_tool(
        self,
        name: str,
        arguments: Mapping[str, object],
    ) -> McpCallResult:
        """调用一个远端 MCP 工具。

        Args:
            name: MCP 服务声明的原始工具名。
            arguments: 结构化调用参数。

        Returns:
            标准化工具调用结果；远端工具失败通过 ``is_error`` 表示。
        """
        async with self._operation():
            self._ensure_capability("tools", self._capabilities.tools)
            if not name.strip():
                raise ValueError("MCP tool name must not be empty")
            payload = await self._execute(
                "call_tool",
                self._session.call_tool(name, dict(arguments)),
                self._config.limits.request_timeout_seconds,
            )
            self._enforce_content_limit(payload, ("content",), "call_tool")
            return self._mapper.map_call_result(payload)

    async def list_resources(self) -> tuple[McpResourceDefinition, ...]:
        """读取全部资源定义，并强制执行分页与条目上限。"""
        async with self._operation():
            self._ensure_capability("resources", self._capabilities.resources)
            return await self._paginate(
                "list_resources",
                self._list_resource_page,
                lambda page: page.items,
                lambda page: page.next_cursor,
            )

    async def read_resource(self, uri: str) -> McpResourceResult:
        """读取一个 MCP 资源。

        Args:
            uri: 服务发现阶段返回的资源 URI。

        Returns:
            标准化资源内容。
        """
        async with self._operation():
            self._ensure_capability("resources", self._capabilities.resources)
            if not uri.strip():
                raise ValueError("MCP resource uri must not be empty")
            payload = await self._execute(
                "read_resource",
                self._session.read_resource(uri),
                self._config.limits.request_timeout_seconds,
            )
            self._enforce_content_limit(
                payload,
                ("contents", "content"),
                "read_resource",
            )
            return self._mapper.map_resource_result(payload)

    async def list_resource_templates(self) -> tuple[McpResourceTemplateDefinition, ...]:
        """读取全部参数化资源模板，并强制执行分页与条目上限。"""
        async with self._operation():
            self._ensure_capability("resources", self._capabilities.resources)
            return await self._paginate(
                "list_resource_templates",
                self._list_resource_template_page,
                lambda page: page.items,
                lambda page: page.next_cursor,
            )

    async def list_prompts(self) -> tuple[McpPromptDefinition, ...]:
        """读取全部 Prompt，并强制执行分页与条目上限。"""
        async with self._operation():
            self._ensure_capability("prompts", self._capabilities.prompts)
            return await self._paginate(
                "list_prompts",
                self._list_prompt_page,
                lambda page: page.items,
                lambda page: page.next_cursor,
            )

    async def get_prompt(
        self,
        name: str,
        arguments: Mapping[str, object],
    ) -> McpPromptResult:
        """使用结构化参数获取一个 MCP Prompt。

        Args:
            name: MCP 服务声明的 Prompt 名称。
            arguments: Prompt 参数。

        Returns:
            标准化角色消息列表。
        """
        async with self._operation():
            self._ensure_capability("prompts", self._capabilities.prompts)
            if not name.strip():
                raise ValueError("MCP prompt name must not be empty")
            payload = await self._execute(
                "get_prompt",
                self._session.get_prompt(name, dict(arguments)),
                self._config.limits.request_timeout_seconds,
            )
            self._enforce_prompt_content_limit(payload)
            return self._mapper.map_prompt_result(payload)

    async def catalog(self) -> McpCatalog:
        """按已协商能力发现工具、资源与 Prompt，返回稳定目录快照。"""
        async with self._operation():
            return McpCatalog(
                tools=(
                    await self._paginate(
                        "list_tools",
                        self._list_tool_page,
                        lambda page: page.items,
                        lambda page: page.next_cursor,
                    )
                    if self._capabilities.tools is not False
                    else ()
                ),
                resources=(
                    await self._paginate(
                        "list_resources",
                        self._list_resource_page,
                        lambda page: page.items,
                        lambda page: page.next_cursor,
                    )
                    if self._capabilities.resources is not False
                    else ()
                ),
                resource_templates=(
                    await self._paginate(
                        "list_resource_templates",
                        self._list_resource_template_page,
                        lambda page: page.items,
                        lambda page: page.next_cursor,
                    )
                    if self._capabilities.resources is not False
                    else ()
                ),
                prompts=(
                    await self._paginate(
                        "list_prompts",
                        self._list_prompt_page,
                        lambda page: page.items,
                        lambda page: page.next_cursor,
                    )
                    if self._capabilities.prompts is not False
                    else ()
                ),
            )

    async def aclose(self) -> None:
        """停止新操作，等待活跃操作排空，再按所有权配置关闭 Session。"""
        async with self._lifecycle_lock:
            async with self._operation_lock:
                if self._closed:
                    return
                self._closed = True
                self._started = False
            await self._operations_drained.wait()
            if self._config.owns_session:
                await self._execute(
                    "close",
                    self._session.aclose(),
                    self._config.limits.close_timeout_seconds,
                )

    async def _list_tool_page(self, cursor: str | None) -> McpToolPage:
        payload = await self._execute(
            "list_tools",
            self._session.list_tools(cursor=cursor),
            self._config.limits.request_timeout_seconds,
        )
        self._enforce_page_item_limit(payload, ("tools",), "list_tools")
        return self._mapper.map_tool_page(payload)

    async def _list_resource_page(self, cursor: str | None) -> McpResourcePage:
        payload = await self._execute(
            "list_resources",
            self._session.list_resources(cursor=cursor),
            self._config.limits.request_timeout_seconds,
        )
        self._enforce_page_item_limit(payload, ("resources",), "list_resources")
        return self._mapper.map_resource_page(payload)

    async def _list_resource_template_page(
        self,
        cursor: str | None,
    ) -> McpResourceTemplatePage:
        payload = await self._execute(
            "list_resource_templates",
            self._session.list_resource_templates(cursor=cursor),
            self._config.limits.request_timeout_seconds,
        )
        self._enforce_page_item_limit(
            payload,
            ("resourceTemplates", "resource_templates"),
            "list_resource_templates",
        )
        return self._mapper.map_resource_template_page(payload)

    async def _list_prompt_page(self, cursor: str | None) -> McpPromptPage:
        payload = await self._execute(
            "list_prompts",
            self._session.list_prompts(cursor=cursor),
            self._config.limits.request_timeout_seconds,
        )
        self._enforce_page_item_limit(payload, ("prompts",), "list_prompts")
        return self._mapper.map_prompt_page(payload)

    async def _paginate(
        self,
        operation: str,
        fetch_page: Callable[[str | None], Awaitable[PageT]],
        page_items: Callable[[PageT], tuple[ItemT, ...]],
        page_cursor: Callable[[PageT], str | None],
    ) -> tuple[ItemT, ...]:
        result: list[ItemT] = []
        seen_cursors: set[str] = set()
        cursor: str | None = None
        for _ in range(self._config.limits.max_pages):
            page = await fetch_page(cursor)
            items = page_items(page)
            if len(result) + len(items) > self._config.limits.max_items:
                raise McpPaginationLimitError(operation, "max_items")
            result.extend(items)
            cursor = page_cursor(page)
            if cursor is None:
                return tuple(result)
            if cursor in seen_cursors:
                raise McpPaginationLimitError(operation, "repeated cursor")
            seen_cursors.add(cursor)
        raise McpPaginationLimitError(operation, "max_pages")

    def _enforce_page_item_limit(
        self,
        payload: object,
        names: tuple[str, ...],
        operation: str,
    ) -> None:
        """在 Mapper 再次物化 DTO 前拒绝明显过大的单页列表。"""
        size = self._raw_sequence_size(payload, names)
        if size is not None and size > self._config.limits.max_items:
            raise McpPaginationLimitError(operation, "max_items")

    def _enforce_content_limit(
        self,
        payload: object,
        names: tuple[str, ...],
        operation: str,
    ) -> None:
        """在 Mapper 复制内容块前执行单次响应块数边界。"""
        size = self._raw_sequence_size(payload, names)
        if size is not None and size > self._config.limits.max_content_blocks:
            raise McpResponseLimitError(operation, "max_content_blocks")

    def _enforce_prompt_content_limit(self, payload: object) -> None:
        """限制 Prompt 消息数及其中内容块的合计数量。"""
        messages = self._raw_field(payload, ("messages",))
        if not self._is_sequence(messages):
            return
        if len(messages) > self._config.limits.max_content_blocks:
            raise McpResponseLimitError("get_prompt", "max_content_blocks")
        total_blocks = 0
        for message in messages:
            content = self._raw_field(message, ("content",))
            total_blocks += len(content) if self._is_sequence(content) else 1
            if total_blocks > self._config.limits.max_content_blocks:
                raise McpResponseLimitError("get_prompt", "max_content_blocks")

    @classmethod
    def _raw_sequence_size(
        cls,
        payload: object,
        names: tuple[str, ...],
    ) -> int | None:
        value = cls._raw_field(payload, names)
        return len(value) if cls._is_sequence(value) else None

    @staticmethod
    def _raw_field(payload: object, names: tuple[str, ...]) -> object:
        if isinstance(payload, Mapping):
            for name in names:
                if name in payload:
                    return payload[name]
            return _MISSING
        for name in names:
            if hasattr(payload, name):
                return getattr(payload, name)
        return _MISSING

    @staticmethod
    def _is_sequence(value: object) -> TypeGuard[Sequence[object]]:
        return isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray))

    def _ensure_ready(self) -> None:
        if self._closed:
            raise McpLifecycleError("MCP connection is closed")
        if not self._started:
            raise McpLifecycleError("MCP connection has not been started")

    def _ensure_capability(self, name: str, supported: bool | None) -> None:
        """拒绝初始化结果明确未声明的服务端能力。"""
        self._ensure_ready()
        if supported is False:
            raise McpCapabilityNotSupportedError(self._config.name, name)

    @asynccontextmanager
    async def _operation(self) -> AsyncIterator[None]:
        """固定一次直接连接操作，并让关闭等待其完整结束。"""
        async with self._operation_lock:
            self._ensure_ready()
            self._active_operations += 1
            self._operations_drained.clear()
        try:
            yield
        finally:
            async with self._operation_lock:
                self._active_operations -= 1
                if self._active_operations == 0:
                    self._operations_drained.set()

    @staticmethod
    async def _execute(operation: str, awaitable: Awaitable[object], timeout: float) -> object:
        try:
            return await asyncio.wait_for(awaitable, timeout=timeout)
        except (asyncio.TimeoutError, TimeoutError):
            raise McpTimeoutError(operation, timeout) from None
        except McpError:
            raise
        except Exception:
            # 不串联供应商异常，避免异常链和日志包含请求头、密钥或远端自由文本。
            raise McpTransportError(operation) from None
