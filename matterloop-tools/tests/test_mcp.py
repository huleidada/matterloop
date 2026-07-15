"""MCP 注入式连接、能力操作和工具适配离线测试。"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from types import SimpleNamespace

import matterloop_tools.mcp.tool_adapter as mcp_tool_adapter_module
import pytest
from matterloop_tools import ToolContext, ToolRegistry
from matterloop_tools.mcp import (
    McpCallResult,
    McpCapabilityNotSupportedError,
    McpCatalogStaleError,
    McpConfigurationError,
    McpContent,
    McpContentKind,
    McpLifecycleError,
    McpLimits,
    McpPaginationLimitError,
    McpRemoteError,
    McpResponseLimitError,
    McpServerConfig,
    McpServerConnection,
    McpServerRegistry,
    McpTimeoutError,
    McpToolAdapter,
    McpToolDefinition,
    McpToolNameCollisionError,
    McpTransportError,
    StructuralMcpResponseMapper,
    safe_mcp_tool_name,
)


class StaticMcpToolCaller:
    """为工具适配器返回固定结果的最小调用入口。"""

    def __init__(self, result: McpCallResult) -> None:
        self._result = result

    async def call_tool(
        self,
        server_name: str,
        tool_name: str,
        arguments: Mapping[str, object],
        *,
        catalog_token: str | None = None,
    ) -> McpCallResult:
        """返回测试预置结果。"""
        del server_name, tool_name, arguments, catalog_token
        return self._result


def build_static_tool_adapter(
    result: McpCallResult,
    *,
    max_result_characters: int,
    max_content_blocks: int = 256,
) -> McpToolAdapter:
    """构造无需 MCP Session 的结果渲染测试适配器。"""
    return McpToolAdapter(
        StaticMcpToolCaller(result),
        "bounded",
        "bounded",
        McpToolDefinition("large-result"),
        max_result_characters=max_result_characters,
        max_content_blocks=max_content_blocks,
    )


class FakeMcpSession:
    """只返回内存响应的 Session Adapter。"""

    def __init__(self, *, label: str = "ok") -> None:
        self.label = label
        self.initialized = False
        self.closed = False
        self.calls: list[tuple[str, Mapping[str, object]]] = []
        self.tool_pages: dict[str | None, object] = {
            None: {
                "tools": [
                    {
                        "name": "echo.text",
                        "description": "回显文本",
                        "inputSchema": {
                            "type": "object",
                            "properties": {"text": {"type": "string"}},
                            "required": ["text"],
                        },
                    }
                ]
            }
        }
        self.resource_pages: dict[str | None, object] = {
            None: {
                "resources": [
                    {
                        "uri": "memory://guide",
                        "name": "guide",
                        "mimeType": "text/plain",
                    }
                ]
            }
        }
        self.resource_template_pages: dict[str | None, object] = {
            None: {
                "resourceTemplates": [
                    {
                        "uriTemplate": "memory://documents/{document_id}",
                        "name": "document",
                        "mimeType": "text/plain",
                    }
                ]
            }
        }
        self.prompt_pages: dict[str | None, object] = {
            None: {
                "prompts": [
                    {
                        "name": "summarize",
                        "description": "总结内容",
                        "arguments": [{"name": "topic", "required": True}],
                    }
                ]
            }
        }
        self.call_started: asyncio.Event | None = None
        self.release_call: asyncio.Event | None = None
        self.raise_call_error = False
        self.tool_is_error = False

    async def initialize(self) -> object:
        self.initialized = True
        return {"protocolVersion": "test"}

    async def list_tools(self, *, cursor: str | None = None) -> object:
        return self.tool_pages[cursor]

    async def call_tool(self, name: str, arguments: Mapping[str, object]) -> object:
        self.calls.append((name, dict(arguments)))
        if self.call_started is not None:
            self.call_started.set()
        if self.release_call is not None:
            await self.release_call.wait()
        if self.raise_call_error:
            raise RuntimeError("Authorization: Bearer secret-value")
        return {
            "content": [{"type": "text", "text": self.label}],
            "structuredContent": {"source": "fake"},
            "isError": self.tool_is_error,
            "_meta": {"private": "not-forwarded"},
        }

    async def list_resources(self, *, cursor: str | None = None) -> object:
        return self.resource_pages[cursor]

    async def read_resource(self, uri: str) -> object:
        return {
            "contents": [
                {
                    "type": "resource",
                    "resource": {"uri": uri, "text": "企业指南", "mimeType": "text/plain"},
                }
            ]
        }

    async def list_resource_templates(self, *, cursor: str | None = None) -> object:
        return self.resource_template_pages[cursor]

    async def list_prompts(self, *, cursor: str | None = None) -> object:
        return self.prompt_pages[cursor]

    async def get_prompt(self, name: str, arguments: Mapping[str, object]) -> object:
        return {
            "description": name,
            "messages": [
                {
                    "role": "user",
                    "content": {"type": "text", "text": f"总结 {arguments['topic']}"},
                }
            ],
        }

    async def aclose(self) -> None:
        self.closed = True


class FakePydanticMapping:
    """模拟 MCP SDK 嵌套 Pydantic 模型。"""

    def model_dump(self, *, by_alias: bool, exclude_none: bool) -> object:
        """返回带别名的普通映射。"""
        assert by_alias
        assert exclude_none
        return {"readOnlyHint": True}


class FakeAnyUrl:
    """模拟官方 SDK 使用的 Pydantic AnyUrl。"""

    def __init__(self, value: str) -> None:
        self._value = value

    def __str__(self) -> str:
        return self._value


def build_connection(
    session: FakeMcpSession,
    *,
    name: str = "knowledge",
    namespace: str = "knowledge",
    limits: McpLimits | None = None,
    owns_session: bool = True,
) -> McpServerConnection:
    """构造测试 MCP 连接。"""
    return McpServerConnection(
        session,
        McpServerConfig(
            name=name,
            tool_namespace=namespace,
            limits=limits or McpLimits(),
            owns_session=owns_session,
        ),
    )


async def test_mcp_discovers_and_operates_tools_resources_and_prompts() -> None:
    session = FakeMcpSession(label="remote-result")
    # 属性对象模拟官方 SDK 的 Pydantic 响应，验证 Mapper 不依赖具体 SDK 类型。
    session.tool_pages[None] = SimpleNamespace(
        tools=[
            SimpleNamespace(
                name="echo.text",
                description="回显文本",
                inputSchema={"type": "object"},
            )
        ],
        nextCursor=None,
    )
    registry = McpServerRegistry()
    await registry.register(build_connection(session))

    catalog = await registry.catalog("knowledge")
    resource = await registry.read_resource("knowledge", "memory://guide")
    prompt = await registry.get_prompt("knowledge", "summarize", {"topic": "MCP"})
    adapters = await registry.discover_tools("knowledge")
    tool_registry = ToolRegistry(adapters)
    result = await tool_registry.invoke(
        "mcp__knowledge__echo_text",
        {"text": "hello"},
        context=ToolContext("run-1"),
    )

    assert session.initialized
    assert catalog.tools[0].name == "echo.text"
    assert catalog.resources[0].uri == "memory://guide"
    assert catalog.resource_templates[0].uri_template == "memory://documents/{document_id}"
    assert catalog.prompts[0].arguments[0].required
    assert resource.contents[0].text == "企业指南"
    assert prompt.messages[0].content[0].text == "总结 MCP"
    assert result.content == 'remote-result\n{"source":"fake"}'
    assert result.metadata == {
        "mcp_server": "knowledge",
        "mcp_tool": "echo.text",
        "content_blocks": 1,
        "truncated": False,
    }
    assert session.calls == [("echo.text", {"text": "hello"})]
    await tool_registry.aclose()
    await registry.aclose()
    assert session.closed


async def test_mcp_catalog_respects_explicit_server_capabilities() -> None:
    """tools-only 服务不应被探测未声明的资源和 Prompt 能力。"""

    class ToolsOnlySession(FakeMcpSession):
        async def initialize(self) -> object:
            self.initialized = True
            return {"capabilities": {"tools": {}}}

        async def list_resources(self, *, cursor: str | None = None) -> object:
            del cursor
            raise AssertionError("resources capability must not be queried")

        async def list_resource_templates(self, *, cursor: str | None = None) -> object:
            del cursor
            raise AssertionError("resource templates capability must not be queried")

        async def list_prompts(self, *, cursor: str | None = None) -> object:
            del cursor
            raise AssertionError("prompts capability must not be queried")

    session = ToolsOnlySession()
    registry = McpServerRegistry()
    connection = build_connection(session)
    await registry.register(connection)

    catalog = await registry.catalog("knowledge")

    assert connection.capabilities.tools is True
    assert connection.capabilities.resources is False
    assert tuple(tool.name for tool in catalog.tools) == ("echo.text",)
    assert catalog.resources == ()
    assert catalog.resource_templates == ()
    assert catalog.prompts == ()
    with pytest.raises(McpCapabilityNotSupportedError, match="resources"):
        await registry.list_resources("knowledge")
    await registry.aclose()


def test_mcp_mapper_accepts_optional_none_and_nested_pydantic_models() -> None:
    """官方 SDK 的可选字段为 None 时应按未声明处理。"""
    mapper = StructuralMcpResponseMapper()

    tool_page = mapper.map_tool_page(
        SimpleNamespace(
            tools=[
                SimpleNamespace(
                    name="safe",
                    description=None,
                    inputSchema={"type": "object"},
                    outputSchema=None,
                    annotations=FakePydanticMapping(),
                )
            ],
            nextCursor=None,
        )
    )
    call_result = mapper.map_call_result(
        SimpleNamespace(
            content=[
                SimpleNamespace(
                    type="resource_link",
                    uri=FakeAnyUrl("memory://linked"),
                    mimeType="text/plain",
                    meta=None,
                ),
                SimpleNamespace(
                    type="resource",
                    resource=SimpleNamespace(
                        uri=FakeAnyUrl("memory://embedded"),
                        text="embedded body",
                        mimeType="text/plain",
                        meta=None,
                    ),
                    meta=None,
                ),
            ],
            structuredContent=None,
            isError=False,
            meta=None,
        )
    )
    resource_page = mapper.map_resource_page(
        SimpleNamespace(
            resources=[
                SimpleNamespace(
                    uri=FakeAnyUrl("memory://guide"),
                    name="guide",
                    description=None,
                    mimeType=None,
                    size=None,
                    meta=None,
                )
            ],
            nextCursor=None,
        )
    )
    resource_result = mapper.map_resource_result(
        SimpleNamespace(
            contents=[
                SimpleNamespace(
                    uri=FakeAnyUrl("memory://text"),
                    text="resource body",
                    mimeType="text/plain",
                    meta=None,
                ),
                SimpleNamespace(
                    uri=FakeAnyUrl("memory://blob"),
                    blob="YmluYXJ5",
                    mimeType="application/octet-stream",
                    meta=None,
                ),
            ],
            meta=None,
        )
    )
    prompt_page = mapper.map_prompt_page(
        SimpleNamespace(
            prompts=[
                SimpleNamespace(
                    name="summary",
                    description=None,
                    arguments=[SimpleNamespace(name="topic", description=None, required=None)],
                )
            ],
            nextCursor=None,
        )
    )

    assert tool_page.items[0].description == ""
    assert tool_page.items[0].annotations == {"readOnlyHint": True}
    assert call_result.structured_content == {}
    assert call_result.metadata == {}
    assert call_result.content[0].uri == "memory://linked"
    assert call_result.content[1].text == "embedded body"
    assert resource_page.items[0].uri == "memory://guide"
    assert resource_result.contents[0].text == "resource body"
    assert resource_result.contents[0].uri == "memory://text"
    assert resource_result.contents[1].data == "YmluYXJ5"
    assert resource_result.contents[1].uri == "memory://blob"
    assert prompt_page.items[0].description == ""
    assert not prompt_page.items[0].arguments[0].required


async def test_mcp_follows_cursors_and_enforces_page_limit() -> None:
    session = FakeMcpSession()
    session.tool_pages = {
        None: {"tools": [{"name": "one"}], "nextCursor": "page-2"},
        "page-2": {"tools": [{"name": "two"}], "nextCursor": "page-3"},
        "page-3": {"tools": [{"name": "three"}]},
    }
    successful_registry = McpServerRegistry()
    await successful_registry.register(
        build_connection(session, limits=McpLimits(max_pages=3, max_items=10))
    )
    tools = await successful_registry.list_tools("knowledge")
    assert tuple(tool.name for tool in tools) == ("one", "two", "three")
    await successful_registry.aclose()

    limited_session = FakeMcpSession()
    limited_session.tool_pages = session.tool_pages
    registry = McpServerRegistry()
    await registry.register(
        build_connection(limited_session, limits=McpLimits(max_pages=2, max_items=10))
    )

    with pytest.raises(McpPaginationLimitError, match="max_pages"):
        await registry.list_tools("knowledge")
    await registry.aclose()


async def test_mcp_resource_templates_use_the_same_pagination_limits() -> None:
    session = FakeMcpSession()
    session.resource_template_pages = {
        None: {
            "resourceTemplates": [{"uriTemplate": "memory://items/{id}", "name": "item"}],
            "nextCursor": "next",
        },
        "next": {
            "resourceTemplates": [{"uriTemplate": "memory://users/{user_id}", "name": "user"}]
        },
    }
    registry = McpServerRegistry()
    await registry.register(build_connection(session))

    templates = await registry.list_resource_templates("knowledge")

    assert tuple(item.name for item in templates) == ("item", "user")
    await registry.aclose()


async def test_mcp_preserves_tool_error_flag_and_limits_rendered_content() -> None:
    session = FakeMcpSession(label="result-is-too-long")
    session.tool_is_error = True
    registry = McpServerRegistry()
    await registry.register(build_connection(session, limits=McpLimits(max_result_characters=6)))
    adapter = (await registry.discover_tools("knowledge"))[0]

    result = await adapter.invoke({}, ToolContext("run-error"))

    assert result.content == "result"
    assert result.is_error
    assert result.metadata["truncated"] is True
    await registry.aclose()


@pytest.mark.parametrize("kind", [McpContentKind.TEXT, McpContentKind.RESOURCE])
async def test_mcp_bounds_large_text_blocks_before_result_aggregation(
    kind: McpContentKind,
) -> None:
    """超大文本与内嵌资源文本只复制字符预算内的前缀。"""
    adapter = build_static_tool_adapter(
        McpCallResult(
            content=(
                McpContent(
                    kind,
                    text="x" * 2_000_000,
                    uri="memory://large" if kind is McpContentKind.RESOURCE else None,
                ),
            )
        ),
        max_result_characters=64,
    )

    result = await adapter.invoke({}, ToolContext(f"run-{kind.value}"))

    assert result.content == "x" * 64
    assert result.metadata["truncated"] is True


async def test_mcp_streams_large_structured_json_within_character_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """结构化结果不得先完整 dumps 后再截断。"""

    def reject_complete_dump(*args: object, **kwargs: object) -> str:
        del args, kwargs
        raise AssertionError("structured MCP result must not use json.dumps")

    monkeypatch.setattr(mcp_tool_adapter_module.json, "dumps", reject_complete_dump)
    adapter = build_static_tool_adapter(
        McpCallResult(structured_content={"payload": "y" * 2_000_000}),
        max_result_characters=48,
    )

    result = await adapter.invoke({}, ToolContext("run-structured"))

    assert len(result.content) == 48
    assert result.content.startswith('{"payload":"yyyy')
    assert result.metadata["truncated"] is True


async def test_mcp_limits_empty_content_blocks_before_rendering() -> None:
    """空内容块也必须消耗块数预算，避免绕过字符上限。"""
    adapter = build_static_tool_adapter(
        McpCallResult(
            content=tuple(McpContent(McpContentKind.TEXT, text="") for _ in range(10_000))
        ),
        max_result_characters=64,
        max_content_blocks=8,
    )

    result = await adapter.invoke({}, ToolContext("run-empty-blocks"))

    assert result.content == ""
    assert result.metadata["truncated"] is True


async def test_mcp_connection_rejects_content_blocks_before_dto_mapping() -> None:
    """连接应在 Mapper 复制超量块之前按本地限制拒绝响应。"""

    class TooManyBlocksSession(FakeMcpSession):
        async def call_tool(self, name: str, arguments: Mapping[str, object]) -> object:
            del name, arguments
            return {"content": [{"type": "text", "text": ""}] * 3}

    registry = McpServerRegistry()
    await registry.register(
        build_connection(
            TooManyBlocksSession(),
            limits=McpLimits(max_content_blocks=2),
        )
    )

    with pytest.raises(McpResponseLimitError, match="max_content_blocks"):
        await registry.call_tool("knowledge", "echo.text", {})
    await registry.aclose()


async def test_mcp_rejects_repeated_cursor_and_item_overflow() -> None:
    repeated = FakeMcpSession()
    repeated.tool_pages = {
        None: {"tools": [{"name": "one"}], "nextCursor": "same"},
        "same": {"tools": [], "nextCursor": "same"},
    }
    repeated_registry = McpServerRegistry()
    await repeated_registry.register(build_connection(repeated))
    with pytest.raises(McpPaginationLimitError, match="repeated cursor"):
        await repeated_registry.list_tools("knowledge")
    await repeated_registry.aclose()

    overflow = FakeMcpSession()
    overflow.tool_pages[None] = {"tools": [{"name": "one"}, {"name": "two"}]}
    overflow_registry = McpServerRegistry()
    await overflow_registry.register(build_connection(overflow, limits=McpLimits(max_items=1)))
    with pytest.raises(McpPaginationLimitError, match="max_items"):
        await overflow_registry.list_tools("knowledge")
    await overflow_registry.aclose()


async def test_mcp_standardizes_transport_and_remote_errors_without_secret_text() -> None:
    session = FakeMcpSession()
    session.raise_call_error = True
    registry = McpServerRegistry()
    await registry.register(build_connection(session))

    with pytest.raises(McpTransportError) as captured:
        await registry.call_tool("knowledge", "echo.text", {})
    assert "secret-value" not in str(captured.value)

    session.raise_call_error = False
    session.tool_pages[None] = {
        "error": {"code": 403, "message": "Authorization: Bearer secret-value"}
    }
    with pytest.raises(McpRemoteError) as remote:
        await registry.list_tools("knowledge")
    assert "secret-value" not in str(remote.value)
    assert "403" in str(remote.value)
    await registry.aclose()


async def test_mcp_applies_request_timeout() -> None:
    session = FakeMcpSession()
    session.release_call = asyncio.Event()
    registry = McpServerRegistry()
    await registry.register(
        build_connection(
            session,
            limits=McpLimits(request_timeout_seconds=0.01),
        )
    )

    with pytest.raises(McpTimeoutError, match="call_tool"):
        await registry.call_tool("knowledge", "echo.text", {})
    await registry.aclose()


async def test_mcp_detects_safe_tool_name_collision() -> None:
    session = FakeMcpSession()
    session.tool_pages[None] = {
        "tools": [
            {"name": "read.file", "inputSchema": {"type": "object"}},
            {"name": "read/file", "inputSchema": {"type": "object"}},
        ]
    }
    registry = McpServerRegistry()
    await registry.register(build_connection(session))

    with pytest.raises(McpToolNameCollisionError, match="mcp__knowledge__read_file"):
        await registry.discover_tools("knowledge")
    await registry.aclose()


def test_mcp_tool_name_is_deterministic_for_non_ascii_names() -> None:
    first = safe_mcp_tool_name("知识服务", "读取文档")

    assert first == safe_mcp_tool_name("知识服务", "读取文档")
    assert first.startswith("mcp__u_")
    assert len(first) <= 64


async def test_mcp_honors_injected_session_ownership() -> None:
    external_session = FakeMcpSession()
    registry = McpServerRegistry()
    await registry.register(build_connection(external_session, owns_session=False))
    await registry.aclose()

    assert external_session.initialized
    assert not external_session.closed


async def test_mcp_registry_reports_typed_lifecycle_error_after_close() -> None:
    registry = McpServerRegistry()
    await registry.aclose()

    with pytest.raises(McpLifecycleError, match="registry is closed"):
        await registry.list_tools("knowledge")
    session = FakeMcpSession()
    with pytest.raises(McpLifecycleError, match="registry is closed"):
        await registry.register(build_connection(session))
    assert not session.initialized


async def test_direct_mcp_connection_close_waits_for_active_call() -> None:
    """公共 Connection 直接使用时也必须保护活跃 Session 请求。"""
    session = FakeMcpSession(label="direct")
    session.call_started = asyncio.Event()
    session.release_call = asyncio.Event()
    connection = build_connection(session)
    await connection.start()

    invocation = asyncio.create_task(connection.call_tool("echo.text", {}))
    await session.call_started.wait()
    close_task = asyncio.create_task(connection.aclose())
    await asyncio.sleep(0)

    assert not close_task.done()
    assert not session.closed
    session.release_call.set()
    result = await invocation
    await close_task

    assert result.content[0].text == "direct"
    assert session.closed
    with pytest.raises(McpLifecycleError, match="closed"):
        await connection.call_tool("echo.text", {})


async def test_mcp_hot_replace_keeps_old_call_lease_until_completion() -> None:
    old_session = FakeMcpSession(label="old")
    old_session.call_started = asyncio.Event()
    old_session.release_call = asyncio.Event()
    new_session = FakeMcpSession(label="new")
    registry = McpServerRegistry()
    await registry.register(build_connection(old_session))
    adapter = (await registry.discover_tools("knowledge"))[0]

    old_call = asyncio.create_task(adapter.invoke({}, ToolContext("run-old")))
    await old_session.call_started.wait()
    await registry.replace("knowledge", build_connection(new_session))

    assert not old_session.closed
    assert new_session.initialized
    with pytest.raises(McpCatalogStaleError, match="rediscover"):
        await adapter.invoke({}, ToolContext("run-stale"))
    fresh_adapter = (await registry.discover_tools("knowledge"))[0]
    new_result = await fresh_adapter.invoke({}, ToolContext("run-new"))
    old_session.release_call.set()
    old_result = await old_call

    assert old_result.content.startswith("old")
    assert new_result.content.startswith("new")
    assert old_session.closed
    await registry.aclose()
    assert new_session.closed


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("max_pages", True),
        ("max_items", 1.5),
        ("max_content_blocks", True),
        ("max_result_characters", 0),
        ("request_timeout_seconds", "30"),
    ],
)
def test_mcp_limits_reject_wrong_types_and_nonpositive_values(
    field: str,
    value: object,
) -> None:
    """资源限制不能依赖类型注解隐式校验。"""
    with pytest.raises(McpConfigurationError, match="positive"):
        McpLimits(**{field: value})  # type: ignore[arg-type]


async def test_mcp_close_waits_for_active_operation_to_drain() -> None:
    session = FakeMcpSession(label="active")
    session.call_started = asyncio.Event()
    session.release_call = asyncio.Event()
    registry = McpServerRegistry()
    await registry.register(build_connection(session))
    adapter = (await registry.discover_tools("knowledge"))[0]

    invocation = asyncio.create_task(adapter.invoke({}, ToolContext("run-active")))
    await session.call_started.wait()
    close_task = asyncio.create_task(registry.aclose())
    await asyncio.sleep(0)

    assert not close_task.done()
    assert not session.closed
    session.release_call.set()
    result = await invocation
    await close_task

    assert result.content.startswith("active")
    assert session.closed


async def test_mcp_close_waits_for_retired_connection_close_to_finish() -> None:
    """排空信号必须覆盖 RuntimeContainer 在租约退出时执行的异步关闭。"""

    class SlowCloseSession(FakeMcpSession):
        def __init__(self) -> None:
            super().__init__(label="slow-close")
            self.close_started = asyncio.Event()
            self.release_close = asyncio.Event()

        async def aclose(self) -> None:
            self.close_started.set()
            await self.release_close.wait()
            self.closed = True

    session = SlowCloseSession()
    session.call_started = asyncio.Event()
    session.release_call = asyncio.Event()
    registry = McpServerRegistry()
    await registry.register(build_connection(session))
    adapter = (await registry.discover_tools("knowledge"))[0]

    invocation = asyncio.create_task(adapter.invoke({}, ToolContext("run-slow-close")))
    await session.call_started.wait()
    close_task = asyncio.create_task(registry.aclose())
    session.release_call.set()
    await session.close_started.wait()

    assert not close_task.done()
    session.release_close.set()
    result = await invocation
    await close_task

    assert result.content.startswith("slow-close")
    assert session.closed
