"""把本地函数声明为统一 MatterLoop 工具的便捷装饰器。"""

from __future__ import annotations

import inspect
import json
from collections.abc import Callable, Mapping
from typing import Any, TypeVar

from matterloop_tools.base import ToolContext, ToolEffect, ToolResult, ToolSpec
from matterloop_tools.capabilities import (
    MCP_CAPABILITY_ANNOTATION,
    CapabilitySpec,
    ToolDescriptor,
    ToolOrigin,
)
from matterloop_tools.errors import ToolConfigurationError

_F = TypeVar("_F", bound=Callable[..., object])


class FunctionTool:
    """将同步或异步 Python 函数适配为 Tool 协议。"""

    def __init__(
        self,
        function: Callable[..., object],
        *,
        name: str,
        description: str,
        input_schema: Mapping[str, object],
        default_effect: ToolEffect,
        capability_spec: CapabilitySpec | None,
        origin: ToolOrigin,
        tool_ref: str | None,
        output_schema: Mapping[str, object] | None,
        annotations: Mapping[str, object] | None,
    ) -> None:
        self._function = function
        self._signature = inspect.signature(function)
        self._spec = ToolSpec(
            name=name,
            description=description,
            input_schema=input_schema,
            default_effect=default_effect,
        )
        self._descriptor = ToolDescriptor(
            tool_ref=tool_ref or f"{origin.value}:{name}",
            origin=origin,
            spec=self._spec,
            capability=capability_spec,
            output_schema=output_schema,
            annotations=annotations or {},
        )

    @property
    def spec(self) -> ToolSpec:
        """返回模型可见的工具规范。"""
        return self._spec

    @property
    def descriptor(self) -> ToolDescriptor:
        """返回来源与能力描述。"""
        return self._descriptor

    def with_capability(self, capability_spec: CapabilitySpec) -> FunctionTool:
        """返回使用新能力声明的等价工具实例。"""
        return FunctionTool(
            self._function,
            name=self._spec.name,
            description=self._spec.description,
            input_schema=self._spec.input_schema,
            default_effect=self._spec.default_effect,
            capability_spec=capability_spec,
            origin=self._descriptor.origin,
            tool_ref=self._descriptor.tool_ref,
            output_schema=self._descriptor.output_schema,
            annotations=self._descriptor.annotations,
        )

    async def invoke(
        self,
        arguments: Mapping[str, object],
        context: ToolContext,
    ) -> ToolResult:
        """按函数签名调用本地实现并标准化返回值。"""
        kwargs = dict(arguments)
        if "context" in self._signature.parameters and "context" not in kwargs:
            kwargs["context"] = context
        try:
            self._signature.bind(**kwargs)
        except TypeError as exc:
            raise ToolConfigurationError(f"invalid arguments for {self._spec.name}: {exc}") from exc
        result = self._function(**kwargs)
        if inspect.isawaitable(result):
            result = await result
        return _normalize_result(result)


def tool(
    *,
    description: str,
    input_schema: Mapping[str, object],
    name: str | None = None,
    default_effect: ToolEffect = ToolEffect.UNKNOWN,
    capability_spec: CapabilitySpec | None = None,
    origin: ToolOrigin = ToolOrigin.LOCAL,
    tool_ref: str | None = None,
    output_schema: Mapping[str, object] | None = None,
    annotations: Mapping[str, object] | None = None,
) -> Callable[[_F], FunctionTool]:
    """将本地函数声明为可进入 ToolRegistry 的工具。"""

    def decorate(function: _F) -> FunctionTool:
        declared_capability = getattr(function, "__matterloop_capability__", None)
        declared_origin = getattr(function, "__matterloop_origin__", None)
        return FunctionTool(
            function,
            name=name or function.__name__,
            description=description,
            input_schema=input_schema,
            default_effect=default_effect,
            capability_spec=capability_spec or declared_capability,
            origin=ToolOrigin(declared_origin or origin),
            tool_ref=tool_ref,
            output_schema=output_schema,
            annotations=annotations,
        )

    return decorate


def capability(spec: CapabilitySpec) -> Callable[[Any], Any]:
    """给本地函数或已适配工具附加稳定能力声明。"""

    def decorate(target: Any) -> Any:
        if isinstance(target, FunctionTool):
            return target.with_capability(spec)
        _declare(target, "__matterloop_capability__", spec)
        return target

    return decorate


def mcp_tool(spec: CapabilitySpec) -> Callable[[_F], _F]:
    """标记本地 MCP 函数，并生成可交给 MCP Server 的 namespaced annotations。

    该装饰器不建立 MCP Server，也不接管 transport。宿主仍使用官方 MCP SDK 暴露函数，
    并可从 ``__matterloop_mcp_annotations__`` 读取声明。
    """

    def decorate(function: _F) -> _F:
        _declare(function, "__matterloop_capability__", spec)
        _declare(function, "__matterloop_origin__", ToolOrigin.LOCAL_MCP)
        _declare(
            function,
            "__matterloop_mcp_annotations__",
            {MCP_CAPABILITY_ANNOTATION: dict(spec.to_mapping())},
        )
        return function

    return decorate


def _declare(target: object, name: str, value: object) -> None:
    """给装饰目标附加组合根可读取的声明。"""
    setattr(target, name, value)


def _normalize_result(value: object) -> ToolResult:
    if isinstance(value, ToolResult):
        return value
    if isinstance(value, Mapping):
        structured = dict(value)
        try:
            content = json.dumps(structured, ensure_ascii=False, separators=(",", ":"))
        except (TypeError, ValueError) as exc:
            raise ToolConfigurationError("tool mapping result must be JSON-compatible") from exc
        return ToolResult(content=content, structured_content=structured)
    if value is None:
        return ToolResult("")
    return ToolResult(str(value))
