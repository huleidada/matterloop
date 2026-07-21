"""限制协议、主机、重定向和响应体的异步 HTTP 工具。"""

from __future__ import annotations

from collections.abc import Mapping

import httpx

from matterloop_tools.base import ToolContext, ToolEffect, ToolResult, ToolSpec
from matterloop_tools.errors import ToolConfigurationError, ToolInputError


class HttpTool:
    """仅访问显式主机白名单的异步 HTTP 工具。

    Args:
        allowed_hosts: 精确匹配且不包含端口的主机白名单。
        allowed_methods: 允许的 HTTP 方法，默认只有 GET。
        require_https: 是否拒绝明文 HTTP，默认启用。
        follow_redirects: 是否手动跟随并重新校验重定向目标。
        max_redirects: 最大重定向次数。
        max_timeout_seconds: 请求超时硬上限。
        max_response_bytes: 最多读取的响应体字节数。
        max_request_bytes: 最多发送的文本请求体字节数。
        allowed_headers: 允许调用方设置的请求头名称。
        transport: 测试或自定义网络层使用的 httpx transport。
    """

    def __init__(
        self,
        *,
        allowed_hosts: frozenset[str] | set[str],
        allowed_methods: frozenset[str] | set[str] = frozenset({"GET"}),
        require_https: bool = True,
        follow_redirects: bool = False,
        max_redirects: int = 3,
        max_timeout_seconds: float = 20.0,
        max_response_bytes: int = 2_000_000,
        max_request_bytes: int = 1_000_000,
        allowed_headers: frozenset[str] | set[str] = frozenset(
            {"accept", "content-type", "user-agent"}
        ),
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._allowed_hosts = frozenset(self._normalize_host(host) for host in allowed_hosts)
        if not self._allowed_hosts:
            raise ToolConfigurationError("allowed_hosts must not be empty")
        self._allowed_methods = frozenset(method.upper() for method in allowed_methods)
        if not self._allowed_methods:
            raise ToolConfigurationError("allowed_methods must not be empty")
        if (
            max_redirects < 0
            or min(max_timeout_seconds, max_response_bytes, max_request_bytes) <= 0
        ):
            raise ToolConfigurationError("HTTP limits must be positive")
        self._require_https = require_https
        self._follow_redirects = follow_redirects
        self._max_redirects = max_redirects
        self._max_timeout_seconds = max_timeout_seconds
        self._max_response_bytes = max_response_bytes
        self._max_request_bytes = max_request_bytes
        self._allowed_headers = frozenset(header.lower() for header in allowed_headers)
        # 网络代理、证书路径等必须由调用方通过 transport 显式配置，禁止读取宿主环境。
        self._client = httpx.AsyncClient(
            follow_redirects=False,
            transport=transport,
            trust_env=False,
        )
        self._spec = ToolSpec(
            name="http",
            description="向显式白名单 HTTPS 主机发起受限 HTTP 请求。",
            input_schema={
                "type": "object",
                "properties": {
                    "url": {"type": "string", "format": "uri"},
                    "method": {"type": "string"},
                    "headers": {"type": "object", "additionalProperties": {"type": "string"}},
                    "body": {"type": "string"},
                    "timeout_seconds": {"type": "number", "exclusiveMinimum": 0},
                },
                "required": ["url"],
                "additionalProperties": False,
            },
            default_effect=ToolEffect.WRITE,
            effect_argument="method",
            effect_mapping={"GET": ToolEffect.READ},
            effect_argument_default="GET",
        )

    @property
    def spec(self) -> ToolSpec:
        """返回 HTTP 工具发现信息。"""
        return self._spec

    async def invoke(
        self,
        arguments: Mapping[str, object],
        context: ToolContext,
    ) -> ToolResult:
        """执行一次经过协议、主机和资源边界检查的请求。

        Raises:
            ToolInputError: 参数非法或 URL 越过安全边界。
        """
        del context
        raw_url = arguments.get("url")
        if not isinstance(raw_url, str) or not raw_url:
            raise ToolInputError("url must be a non-empty string")
        try:
            url = self._validate_url(httpx.URL(raw_url))
        except httpx.InvalidURL as exc:
            raise ToolInputError(f"invalid URL: {exc}") from exc
        method_value = arguments.get("method", "GET")
        if not isinstance(method_value, str):
            raise ToolInputError("method must be a string")
        method = method_value.upper()
        if method not in self._allowed_methods:
            raise ToolInputError(f"HTTP method is not allowed: {method}")
        headers = self._headers(arguments.get("headers", {}))
        body_value = arguments.get("body")
        if body_value is not None and not isinstance(body_value, str):
            raise ToolInputError("body must be a string")
        body = None if body_value is None else body_value.encode("utf-8")
        if body is not None and len(body) > self._max_request_bytes:
            raise ToolInputError("body exceeds max_request_bytes")
        timeout_value = arguments.get("timeout_seconds", self._max_timeout_seconds)
        if (
            not isinstance(timeout_value, (int, float))
            or isinstance(timeout_value, bool)
            or timeout_value <= 0
        ):
            raise ToolInputError("timeout_seconds must be a positive number")
        if float(timeout_value) > self._max_timeout_seconds:
            raise ToolInputError("timeout_seconds exceeds configured maximum")
        return await self._request(
            method,
            url,
            headers,
            body,
            timeout_seconds=float(timeout_value),
        )

    async def aclose(self) -> None:
        """关闭底层异步 HTTP 客户端。"""
        await self._client.aclose()

    async def _request(
        self,
        method: str,
        url: httpx.URL,
        headers: dict[str, str],
        body: bytes | None,
        *,
        timeout_seconds: float,
    ) -> ToolResult:
        redirect_count = 0
        current_method = method
        current_body = body
        while True:
            async with self._client.stream(
                current_method,
                url,
                headers=headers,
                content=current_body,
                timeout=timeout_seconds,
            ) as response:
                if response.is_redirect and self._follow_redirects:
                    location = response.headers.get("location")
                    if location is None:
                        raise ToolInputError("redirect response has no Location header")
                    if redirect_count >= self._max_redirects:
                        raise ToolInputError("HTTP redirect limit exceeded")
                    url = self._validate_url(response.url.join(location))
                    redirect_count += 1
                    if response.status_code == 303 or (
                        response.status_code in {301, 302} and current_method == "POST"
                    ):
                        current_method = "GET"
                        current_body = None
                    if current_method not in self._allowed_methods:
                        raise ToolInputError(
                            f"redirect requires a disallowed HTTP method: {current_method}"
                        )
                    continue
                content, truncated = await self._read_response(response)
                encoding = response.encoding or "utf-8"
                try:
                    decoded_content = content.decode(encoding, errors="replace")
                except LookupError:
                    decoded_content = content.decode("utf-8", errors="replace")
                return ToolResult(
                    decoded_content,
                    is_error=response.is_error,
                    metadata={
                        "status_code": response.status_code,
                        "final_url": str(response.url),
                        "content_type": response.headers.get("content-type", ""),
                        "redirects": redirect_count,
                        "truncated": truncated,
                    },
                )

    async def _read_response(self, response: httpx.Response) -> tuple[bytes, bool]:
        content = bytearray()
        async for chunk in response.aiter_bytes():
            remaining = self._max_response_bytes - len(content)
            if len(chunk) > remaining:
                content.extend(chunk[:remaining])
                return bytes(content), True
            content.extend(chunk)
        return bytes(content), False

    def _validate_url(self, url: httpx.URL) -> httpx.URL:
        if url.is_relative_url or not url.host:
            raise ToolInputError("URL must be absolute")
        if url.userinfo:
            raise ToolInputError("URL credentials are not allowed")
        if self._require_https and url.scheme != "https":
            raise ToolInputError("only HTTPS URLs are allowed")
        if not self._require_https and url.scheme not in {"http", "https"}:
            raise ToolInputError("only HTTP and HTTPS URLs are allowed")
        if self._normalize_host(url.host) not in self._allowed_hosts:
            raise ToolInputError(f"HTTP host is not allowed: {url.host}")
        return url

    def _headers(self, value: object) -> dict[str, str]:
        if not isinstance(value, Mapping):
            raise ToolInputError("headers must be an object")
        result: dict[str, str] = {}
        for key, item in value.items():
            if not isinstance(key, str) or not isinstance(item, str):
                raise ToolInputError("header names and values must be strings")
            lowered = key.lower()
            if lowered not in self._allowed_headers:
                raise ToolInputError(f"HTTP header is not allowed: {key}")
            if "\r" in item or "\n" in item:
                raise ToolInputError("HTTP header value contains a newline")
            result[lowered] = item
        return result

    @staticmethod
    def _normalize_host(host: str) -> str:
        value = host.strip().rstrip(".").lower()
        if not value or any(character in value for character in "/:@"):
            raise ToolConfigurationError(f"invalid allowed host: {host}")
        try:
            return value.encode("idna").decode("ascii")
        except UnicodeError as exc:
            raise ToolConfigurationError(f"invalid allowed host: {host}") from exc
