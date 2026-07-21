"""HTTP 工具协议、主机和重定向边界测试。"""

import httpx
import pytest
from matterloop_tools import (
    HttpTool,
    ToolAccessScope,
    ToolContext,
    ToolEffect,
    ToolInputError,
    ToolPermissionDeniedError,
    ToolRegistry,
)


async def test_http_allows_https_host_and_limits_body() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"abcdefgh", request=request)

    tool = HttpTool(
        allowed_hosts={"api.example.com"},
        max_response_bytes=4,
        transport=httpx.MockTransport(handler),
    )
    result = await tool.invoke({"url": "https://api.example.com/data"}, ToolContext("run"))
    await tool.aclose()

    assert result.content == "abcd"
    assert result.metadata["truncated"] is True


async def test_http_classifies_default_get_as_read_and_other_methods_as_write() -> None:
    tool = HttpTool(
        allowed_hosts={"api.example.com"},
        allowed_methods={"GET", "POST", "DELETE"},
    )

    assert tool.spec.effect_for({"url": "https://api.example.com"}) is ToolEffect.READ
    assert (
        tool.spec.effect_for({"url": "https://api.example.com", "method": "GET"}) is ToolEffect.READ
    )
    assert (
        tool.spec.effect_for({"url": "https://api.example.com", "method": "POST"})
        is ToolEffect.WRITE
    )
    assert (
        tool.spec.effect_for({"url": "https://api.example.com", "method": "DELETE"})
        is ToolEffect.WRITE
    )
    await tool.aclose()


async def test_read_only_scope_rejects_http_post_before_network_call() -> None:
    network_calls = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal network_calls
        network_calls += 1
        return httpx.Response(200, text="ok", request=request)

    registry = ToolRegistry(
        [
            HttpTool(
                allowed_hosts={"api.example.com"},
                allowed_methods={"GET", "POST"},
                transport=httpx.MockTransport(handler),
            )
        ]
    )
    context = ToolContext("child", access_scope=ToolAccessScope.READ_ONLY)

    with pytest.raises(ToolPermissionDeniedError):
        await registry.invoke(
            "http",
            {"url": "https://api.example.com/data", "method": "POST", "body": "payload"},
            context=context,
        )

    assert network_calls == 0
    await registry.aclose()


async def test_http_rejects_plaintext_and_unlisted_host() -> None:
    tool = HttpTool(allowed_hosts={"api.example.com"})

    with pytest.raises(ToolInputError, match="HTTPS"):
        await tool.invoke({"url": "http://api.example.com"}, ToolContext("run"))
    with pytest.raises(ToolInputError, match="not allowed"):
        await tool.invoke({"url": "https://evil.example"}, ToolContext("run"))
    with pytest.raises(ToolInputError, match="credentials"):
        await tool.invoke(
            {"url": "https://user:password@api.example.com"},
            ToolContext("run"),
        )
    await tool.aclose()


async def test_http_normalizes_host_case_and_trailing_dot() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="ok", request=request)

    tool = HttpTool(
        allowed_hosts={"API.Example.COM."},
        transport=httpx.MockTransport(handler),
    )
    result = await tool.invoke(
        {"url": "https://api.example.com./data"},
        ToolContext("run"),
    )
    await tool.aclose()

    assert result.content == "ok"


async def test_http_revalidates_every_redirect_target() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            302,
            headers={"location": "https://evil.example/steal"},
            request=request,
        )

    tool = HttpTool(
        allowed_hosts={"api.example.com"},
        follow_redirects=True,
        transport=httpx.MockTransport(handler),
    )

    with pytest.raises(ToolInputError, match="not allowed"):
        await tool.invoke({"url": "https://api.example.com/start"}, ToolContext("run"))
    await tool.aclose()


async def test_http_client_disables_environment_configuration(monkeypatch) -> None:
    """HTTP 客户端不得读取宿主代理或证书环境变量。"""
    original_client = httpx.AsyncClient
    captured: dict[str, object] = {}

    def build_client(*args, **kwargs):
        captured.update(kwargs)
        return original_client(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", build_client)
    tool = HttpTool(allowed_hosts={"api.example.com"})
    await tool.aclose()

    assert captured["trust_env"] is False
