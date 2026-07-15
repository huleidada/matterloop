"""隔离 MCP SDK 版本差异的最小结构协议。"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Protocol, runtime_checkable

from matterloop_tools.mcp.models import (
    McpCallResult,
    McpPromptPage,
    McpPromptResult,
    McpResourcePage,
    McpResourceResult,
    McpResourceTemplatePage,
    McpServerCapabilities,
    McpToolPage,
)


@runtime_checkable
class McpSessionAdapter(Protocol):
    """调用方实现或包装的供应商无关 Session 协议。

    方法返回 ``object`` 是有意的：官方 SDK 的响应类型可能随版本变化，稳定字段由
    ``McpResponseMapper`` 统一提取。Session 的传输创建、鉴权和网络配置完全由宿主负责。
    Sampling、elicitation 和 notifications 等服务端主动能力也由宿主 Session Adapter
    显式配置处理器，本协议不会代替应用执行或批准这些操作。
    """

    async def initialize(self) -> object:
        """完成 MCP 能力协商并返回可忽略的原始响应。"""
        ...

    async def list_tools(self, *, cursor: str | None = None) -> object:
        """读取一页工具定义。"""
        ...

    async def call_tool(self, name: str, arguments: Mapping[str, object]) -> object:
        """调用远端 MCP 工具。"""
        ...

    async def list_resources(self, *, cursor: str | None = None) -> object:
        """读取一页资源定义。"""
        ...

    async def read_resource(self, uri: str) -> object:
        """读取一个 MCP 资源。"""
        ...

    async def list_resource_templates(self, *, cursor: str | None = None) -> object:
        """读取一页参数化资源模板定义。"""
        ...

    async def list_prompts(self, *, cursor: str | None = None) -> object:
        """读取一页 Prompt 定义。"""
        ...

    async def get_prompt(self, name: str, arguments: Mapping[str, object]) -> object:
        """使用参数解析一个 MCP Prompt。"""
        ...

    async def aclose(self) -> None:
        """关闭 Session 及宿主明确转交所有权的底层资源。"""
        ...


@runtime_checkable
class McpResponseMapper(Protocol):
    """将具体 SDK 原始响应映射为 MatterLoop 稳定 DTO。"""

    def map_capabilities(self, payload: object) -> McpServerCapabilities:
        """映射初始化结果中的服务端能力。"""
        ...

    def map_tool_page(self, payload: object) -> McpToolPage:
        """映射工具列表页。"""
        ...

    def map_call_result(self, payload: object) -> McpCallResult:
        """映射工具调用结果。"""
        ...

    def map_resource_page(self, payload: object) -> McpResourcePage:
        """映射资源列表页。"""
        ...

    def map_resource_result(self, payload: object) -> McpResourceResult:
        """映射资源读取结果。"""
        ...

    def map_resource_template_page(self, payload: object) -> McpResourceTemplatePage:
        """映射参数化资源模板列表页。"""
        ...

    def map_prompt_page(self, payload: object) -> McpPromptPage:
        """映射 Prompt 列表页。"""
        ...

    def map_prompt_result(self, payload: object) -> McpPromptResult:
        """映射 Prompt 获取结果。"""
        ...
