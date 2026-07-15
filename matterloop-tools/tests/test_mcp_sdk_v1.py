"""官方 MCP Python SDK v1 可选桥接层离线测试。"""

from __future__ import annotations

import asyncio
import importlib.metadata
import sys
from dataclasses import dataclass
from types import ModuleType

import pytest
from matterloop_tools.mcp import (
    McpConfigurationError,
    McpLifecycleError,
    McpSdkV1SessionAdapter,
)


@dataclass(frozen=True)
class FakePaginatedRequestParams:
    """模拟官方 SDK 的分页参数。"""

    cursor: str | None = None


class FakeClientSession:
    """记录所有签名调用的官方 ClientSession 测试替身。"""

    def __init__(self) -> None:
        self.calls: list[tuple[str, object]] = []

    async def initialize(self) -> object:
        self.calls.append(("initialize", None))
        return {"protocolVersion": "test"}

    async def list_tools(self, *, params: object) -> object:
        self.calls.append(("list_tools", params))
        return {"tools": []}

    async def call_tool(
        self,
        name: str,
        arguments: dict[str, object] | None = None,
    ) -> object:
        self.calls.append(("call_tool", (name, arguments)))
        return {"content": []}

    async def list_resources(self, *, params: object) -> object:
        self.calls.append(("list_resources", params))
        return {"resources": []}

    async def list_resource_templates(self, *, params: object) -> object:
        self.calls.append(("list_resource_templates", params))
        return {"resourceTemplates": []}

    async def read_resource(self, uri: str) -> object:
        self.calls.append(("read_resource", uri))
        return {"contents": []}

    async def list_prompts(self, *, params: object) -> object:
        self.calls.append(("list_prompts", params))
        return {"prompts": []}

    async def get_prompt(
        self,
        name: str,
        arguments: dict[str, str] | None = None,
    ) -> object:
        self.calls.append(("get_prompt", (name, arguments)))
        return {"messages": []}


def install_fake_sdk(
    monkeypatch: pytest.MonkeyPatch,
    *,
    version: str = "1.28.1",
    client_session_type: type[object] = FakeClientSession,
    params_type: type[object] = FakePaginatedRequestParams,
) -> None:
    """向模块表安装不执行网络操作的 MCP SDK 测试替身。"""
    mcp_module = ModuleType("mcp")
    mcp_module.__path__ = []  # type: ignore[attr-defined]
    client_package = ModuleType("mcp.client")
    client_package.__path__ = []  # type: ignore[attr-defined]
    session_module = ModuleType("mcp.client.session")
    session_module.ClientSession = client_session_type  # type: ignore[attr-defined]
    types_module = ModuleType("mcp.types")
    types_module.PaginatedRequestParams = params_type  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "mcp", mcp_module)
    monkeypatch.setitem(sys.modules, "mcp.client", client_package)
    monkeypatch.setitem(sys.modules, "mcp.client.session", session_module)
    monkeypatch.setitem(sys.modules, "mcp.types", types_module)
    monkeypatch.setattr(importlib.metadata, "version", lambda distribution: version)


async def test_sdk_v1_translates_official_session_signatures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install_fake_sdk(monkeypatch)
    session = FakeClientSession()
    adapter = McpSdkV1SessionAdapter(session)

    assert adapter.sdk_version == "1.28.1"
    assert await adapter.initialize() == {"protocolVersion": "test"}
    assert await adapter.list_tools(cursor=None) == {"tools": []}
    assert await adapter.list_resources(cursor="resource-2") == {"resources": []}
    assert await adapter.list_resource_templates(cursor="template-3") == {"resourceTemplates": []}
    assert await adapter.list_prompts(cursor="prompt-4") == {"prompts": []}
    assert await adapter.call_tool("echo", {"text": "hello"}) == {"content": []}
    assert await adapter.read_resource("memory://guide") == {"contents": []}
    assert await adapter.get_prompt("summary", {"topic": "MCP"}) == {"messages": []}

    assert session.calls == [
        ("initialize", None),
        ("list_tools", FakePaginatedRequestParams(cursor=None)),
        ("list_resources", FakePaginatedRequestParams(cursor="resource-2")),
        ("list_resource_templates", FakePaginatedRequestParams(cursor="template-3")),
        ("list_prompts", FakePaginatedRequestParams(cursor="prompt-4")),
        ("call_tool", ("echo", {"text": "hello"})),
        ("read_resource", "memory://guide"),
        ("get_prompt", ("summary", {"topic": "MCP"})),
    ]


async def test_sdk_v1_requires_string_prompt_arguments(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install_fake_sdk(monkeypatch)
    session = FakeClientSession()
    adapter = McpSdkV1SessionAdapter(session)

    with pytest.raises(ValueError, match="must be strings"):
        await adapter.get_prompt("summary", {"limit": 3})
    assert session.calls == []


@pytest.mark.parametrize(
    "version",
    ["1.28.0", "1.29.0rc1", "1.29.0.dev1", "2.0.0", "not-a-version"],
)
def test_sdk_v1_rejects_unsupported_versions(
    monkeypatch: pytest.MonkeyPatch,
    version: str,
) -> None:
    install_fake_sdk(monkeypatch, version=version)

    with pytest.raises(McpConfigurationError, match=r">=1\.28\.1,<2"):
        McpSdkV1SessionAdapter(FakeClientSession())


def test_sdk_v1_reports_missing_optional_dependency(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def missing_version(distribution: str) -> str:
        raise importlib.metadata.PackageNotFoundError(distribution)

    monkeypatch.setattr(importlib.metadata, "version", missing_version)

    with pytest.raises(McpConfigurationError, match="unavailable"):
        McpSdkV1SessionAdapter(object())


def test_sdk_v1_rejects_non_client_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install_fake_sdk(monkeypatch)

    with pytest.raises(McpConfigurationError, match="ClientSession"):
        McpSdkV1SessionAdapter(object())


def test_sdk_v1_rejects_incompatible_client_shape(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class IncompleteClientSession:
        async def initialize(self) -> object:
            return None

    install_fake_sdk(monkeypatch, client_session_type=IncompleteClientSession)

    with pytest.raises(McpConfigurationError, match="list_tools"):
        McpSdkV1SessionAdapter(IncompleteClientSession())


async def test_sdk_v1_close_is_noop_by_default_and_callback_is_idempotent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install_fake_sdk(monkeypatch)
    default_adapter = McpSdkV1SessionAdapter(FakeClientSession())
    await default_adapter.aclose()
    await default_adapter.aclose()
    with pytest.raises(McpLifecycleError, match="closed"):
        await default_adapter.list_tools()

    close_calls = 0

    async def close_callback() -> None:
        nonlocal close_calls
        await asyncio.sleep(0)
        close_calls += 1

    callback_adapter = McpSdkV1SessionAdapter(
        FakeClientSession(),
        close_callback=close_callback,
    )
    await asyncio.gather(callback_adapter.aclose(), callback_adapter.aclose())
    assert close_calls == 1
