"""官方 MCP Python SDK v1 的可选 Session 桥接层。

本模块只在构造适配器时加载并校验可选的 ``mcp`` 发行包，导入
``matterloop_tools.mcp`` 本身不会引入 MCP SDK 运行时依赖。
"""

from __future__ import annotations

import asyncio
import importlib
import importlib.metadata
import re
from collections.abc import Awaitable, Callable, Mapping
from typing import Protocol, cast

from matterloop_tools.mcp.errors import McpConfigurationError, McpLifecycleError

AsyncCloseCallback = Callable[[], Awaitable[None]]

_MINIMUM_RELEASE = (1, 28, 1)
_VERSION_PATTERN = re.compile(r"^(\d+)\.(\d+)\.(\d+)(.*)$")
_REQUIRED_SESSION_METHODS = (
    "initialize",
    "list_tools",
    "call_tool",
    "list_resources",
    "list_resource_templates",
    "read_resource",
    "list_prompts",
    "get_prompt",
)


class _PaginatedRequestParamsFactory(Protocol):
    def __call__(self, *, cursor: str | None = None) -> object: ...


class _ClientSessionV1(Protocol):
    async def initialize(self) -> object: ...

    async def list_tools(self, *, params: object) -> object: ...

    async def call_tool(
        self,
        name: str,
        arguments: dict[str, object] | None = None,
    ) -> object: ...

    async def list_resources(self, *, params: object) -> object: ...

    async def list_resource_templates(self, *, params: object) -> object: ...

    async def read_resource(self, uri: str) -> object: ...

    async def list_prompts(self, *, params: object) -> object: ...

    async def get_prompt(
        self,
        name: str,
        arguments: dict[str, str] | None = None,
    ) -> object: ...


class McpSdkV1SessionAdapter:
    """将已进入异步上下文的官方 ``ClientSession`` 适配为稳定协议。

    Args:
        session: 已由宿主创建、鉴权并进入异步上下文的官方 ``ClientSession``。
        close_callback: 可选异步关闭回调。未提供时 ``aclose`` 仅关闭适配器，
            不退出或关闭宿主拥有的 Session 与 transport。

    Raises:
        McpConfigurationError: 未安装兼容 SDK、版本不在 ``>=1.28.1,<2``，
            或传入对象不符合官方 v1 ``ClientSession`` 形态。

    Notes:
        适配器不创建 transport，不读取环境变量、URL 或凭据，也不声称拥有原始
        Session。需要转交关闭职责时，宿主必须显式传入 ``close_callback``。
    """

    def __init__(
        self,
        session: object,
        *,
        close_callback: AsyncCloseCallback | None = None,
    ) -> None:
        sdk_version, params_factory = _load_sdk_v1(session)
        self._session = cast(_ClientSessionV1, session)
        self._params_factory = params_factory
        self._sdk_version = sdk_version
        self._close_callback = close_callback
        self._close_lock = asyncio.Lock()
        self._closed = False

    @property
    def sdk_version(self) -> str:
        """返回构造时校验通过的 MCP SDK 版本。"""
        return self._sdk_version

    async def initialize(self) -> object:
        """转发 MCP 初始化与能力协商请求。

        Returns:
            官方 SDK 返回的原始初始化结果。

        Raises:
            McpLifecycleError: 适配器已经关闭。
        """
        self._ensure_open()
        return await self._session.initialize()

    async def list_tools(self, *, cursor: str | None = None) -> object:
        """按官方 ``params`` 签名读取一页工具。

        Args:
            cursor: MCP 服务返回的不透明分页游标。

        Returns:
            官方 SDK 返回的原始工具列表页。
        """
        self._ensure_open()
        return await self._session.list_tools(params=self._pagination_params(cursor))

    async def call_tool(self, name: str, arguments: Mapping[str, object]) -> object:
        """按官方关键字参数签名调用 MCP 工具。

        Args:
            name: MCP 服务声明的工具名称。
            arguments: 工具的结构化参数。

        Returns:
            官方 SDK 返回的原始工具结果。
        """
        self._ensure_open()
        return await self._session.call_tool(name, arguments=dict(arguments))

    async def list_resources(self, *, cursor: str | None = None) -> object:
        """按官方 ``params`` 签名读取一页资源。

        Args:
            cursor: MCP 服务返回的不透明分页游标。

        Returns:
            官方 SDK 返回的原始资源列表页。
        """
        self._ensure_open()
        return await self._session.list_resources(params=self._pagination_params(cursor))

    async def list_resource_templates(self, *, cursor: str | None = None) -> object:
        """按官方 ``params`` 签名读取一页资源模板。

        Args:
            cursor: MCP 服务返回的不透明分页游标。

        Returns:
            官方 SDK 返回的原始资源模板列表页。
        """
        self._ensure_open()
        return await self._session.list_resource_templates(params=self._pagination_params(cursor))

    async def read_resource(self, uri: str) -> object:
        """按官方位置参数签名读取 MCP 资源。

        Args:
            uri: MCP 服务发现阶段返回的资源 URI。

        Returns:
            官方 SDK 返回的原始资源内容。
        """
        self._ensure_open()
        return await self._session.read_resource(uri)

    async def list_prompts(self, *, cursor: str | None = None) -> object:
        """按官方 ``params`` 签名读取一页 Prompt。

        Args:
            cursor: MCP 服务返回的不透明分页游标。

        Returns:
            官方 SDK 返回的原始 Prompt 列表页。
        """
        self._ensure_open()
        return await self._session.list_prompts(params=self._pagination_params(cursor))

    async def get_prompt(
        self,
        name: str,
        arguments: Mapping[str, object],
    ) -> object:
        """按官方字符串参数约束获取 MCP Prompt。

        Args:
            name: MCP 服务声明的 Prompt 名称。
            arguments: Prompt 参数；键和值都必须是字符串。

        Returns:
            官方 SDK 返回的原始 Prompt 结果。

        Raises:
            ValueError: 参数键或参数值不是字符串。
            McpLifecycleError: 适配器已经关闭。
        """
        self._ensure_open()
        string_arguments: dict[str, str] = {}
        for key, value in arguments.items():
            if not isinstance(key, str) or not isinstance(value, str):
                raise ValueError("MCP prompt argument names and values must be strings")
            string_arguments[key] = value
        return await self._session.get_prompt(name, arguments=string_arguments)

    async def aclose(self) -> None:
        """幂等关闭适配器，并执行宿主显式注入的关闭回调。

        默认不操作原始 Session。并发或重复调用只会执行一次关闭回调。
        """
        async with self._close_lock:
            if self._closed:
                return
            self._closed = True
            if self._close_callback is not None:
                await self._close_callback()

    def _pagination_params(self, cursor: str | None) -> object:
        return self._params_factory(cursor=cursor)

    def _ensure_open(self) -> None:
        if self._closed:
            raise McpLifecycleError("MCP SDK v1 session adapter is closed")


def _load_sdk_v1(
    session: object,
) -> tuple[str, _PaginatedRequestParamsFactory]:
    try:
        sdk_version = importlib.metadata.version("mcp")
    except importlib.metadata.PackageNotFoundError:
        raise McpConfigurationError("MCP SDK v1 is unavailable; install mcp>=1.28.1,<2") from None
    if not _is_supported_version(sdk_version):
        raise McpConfigurationError("incompatible MCP SDK version; expected mcp>=1.28.1,<2")

    try:
        session_module = importlib.import_module("mcp.client.session")
        types_module = importlib.import_module("mcp.types")
    except (ImportError, ModuleNotFoundError):
        raise McpConfigurationError("incompatible MCP SDK v1 module layout") from None

    client_session_value = getattr(session_module, "ClientSession", None)
    params_factory_value = getattr(types_module, "PaginatedRequestParams", None)
    if not isinstance(client_session_value, type) or not callable(params_factory_value):
        raise McpConfigurationError("incompatible MCP SDK v1 public types")
    client_session_type = cast(type[object], client_session_value)
    if not isinstance(session, client_session_type):
        raise McpConfigurationError("session must be an MCP SDK v1 ClientSession")
    for method_name in _REQUIRED_SESSION_METHODS:
        if not callable(getattr(session, method_name, None)):
            raise McpConfigurationError(
                f"incompatible MCP SDK v1 ClientSession method: {method_name}"
            )
    return sdk_version, cast(_PaginatedRequestParamsFactory, params_factory_value)


def _is_supported_version(value: str) -> bool:
    match = _VERSION_PATTERN.fullmatch(value.strip())
    if match is None:
        return False
    release = tuple(int(match.group(index)) for index in range(1, 4))
    if release[0] != 1 or release < _MINIMUM_RELEASE:
        return False
    suffix = match.group(4).lower().lstrip(".-")
    return not suffix.startswith(("a", "b", "rc", "dev"))
