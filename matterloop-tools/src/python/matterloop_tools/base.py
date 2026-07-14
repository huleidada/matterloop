"""工具公共 DTO、授权决策与结构协议。"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import Enum
from types import MappingProxyType
from typing import Protocol, runtime_checkable


@dataclass(frozen=True, slots=True)
class ToolSpec:
    """描述模型可发现的工具名称、用途和 JSON Schema 输入。"""

    name: str
    description: str
    input_schema: Mapping[str, object]

    def __post_init__(self) -> None:
        """冻结输入 Schema 并拒绝空名称。"""
        if not self.name.strip():
            raise ValueError("tool name must not be empty")
        if not self.description.strip():
            raise ValueError("tool description must not be empty")
        object.__setattr__(self, "input_schema", MappingProxyType(dict(self.input_schema)))


@dataclass(frozen=True, slots=True)
class ToolContext:
    """向工具与权限策略传递最小调用上下文。"""

    run_id: str
    step_id: str | None = None
    metadata: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        """冻结元数据，避免授权后被调用方修改。"""
        if not self.run_id.strip():
            raise ValueError("run_id must not be empty")
        object.__setattr__(self, "metadata", MappingProxyType(dict(self.metadata)))


@dataclass(frozen=True, slots=True)
class ToolResult:
    """工具返回给 Agent 的标准化文本与非敏感元数据。"""

    content: str
    is_error: bool = False
    metadata: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        """冻结结果元数据。"""
        object.__setattr__(self, "metadata", MappingProxyType(dict(self.metadata)))


class PermissionDecision(str, Enum):
    """权限策略对单次工具调用的决策。"""

    ALLOW = "allow"
    DENY = "deny"


@runtime_checkable
class Tool(Protocol):
    """所有可注册工具必须实现的结构协议。"""

    @property
    def spec(self) -> ToolSpec:
        """返回稳定的工具发现信息。"""
        ...

    async def invoke(
        self,
        arguments: Mapping[str, object],
        context: ToolContext,
    ) -> ToolResult:
        """执行一次工具调用。"""
        ...


@runtime_checkable
class ToolAuthorizer(Protocol):
    """在工具实际执行前做细粒度权限判断。"""

    async def authorize(
        self,
        tool_name: str,
        arguments: Mapping[str, object],
        context: ToolContext,
    ) -> PermissionDecision:
        """返回允许或拒绝决策。"""
        ...


class AllowAllToolAuthorizer:
    """显式允许所有调用的默认授权器。"""

    async def authorize(
        self,
        tool_name: str,
        arguments: Mapping[str, object],
        context: ToolContext,
    ) -> PermissionDecision:
        """允许当前调用；参数仅用于满足统一协议。"""
        del tool_name, arguments, context
        return PermissionDecision.ALLOW
