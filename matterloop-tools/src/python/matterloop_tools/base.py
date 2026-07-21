"""工具公共 DTO、授权决策与结构协议。"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from enum import Enum
from math import isfinite
from types import MappingProxyType
from typing import Protocol, runtime_checkable

_MAX_METADATA_DEPTH = 64


class ToolEffect(str, Enum):
    """工具调用对外部世界产生的最高影响等级。"""

    READ = "read"
    COMPUTE = "compute"
    WRITE = "write"
    UNKNOWN = "unknown"


class ToolAccessScope(str, Enum):
    """主 Loop 传递给工具注册表的强制访问范围。"""

    FULL = "full"
    READ_ONLY = "read_only"


@dataclass(frozen=True, slots=True)
class ToolSpec:
    """描述模型可发现的工具接口及其副作用分类。

    ``effect_mapping`` 的键按大小写不敏感方式匹配 ``effect_argument`` 对应的字符串参数。
    参数缺失时可以通过 ``effect_argument_default`` 声明工具自身的调用默认值；未匹配的
    参数始终回退到 ``default_effect``。自定义工具不声明影响时默认为 ``UNKNOWN``，因此
    不会被只读 Agent 调用。

    Args:
        name: 注册表中的唯一工具名称。
        description: 提供给模型的用途说明。
        input_schema: 模型可见的 JSON Schema。
        default_effect: 参数无法匹配时采用的保守影响等级。
        effect_argument: 用于选择影响等级的参数名，例如 ``operation`` 或 ``method``。
        effect_mapping: 参数字符串值到影响等级的映射。
        effect_argument_default: 参数缺失时用于影响判定的工具默认参数值。
    """

    name: str
    description: str
    input_schema: Mapping[str, object]
    default_effect: ToolEffect = ToolEffect.UNKNOWN
    effect_argument: str | None = None
    effect_mapping: Mapping[str, ToolEffect] = field(default_factory=dict)
    effect_argument_default: str | None = None

    def __post_init__(self) -> None:
        """冻结输入 Schema 和影响映射，并拒绝不完整定义。"""
        if not self.name.strip():
            raise ValueError("tool name must not be empty")
        if not self.description.strip():
            raise ValueError("tool description must not be empty")
        try:
            default_effect = ToolEffect(self.default_effect)
        except ValueError as exc:
            raise ValueError("default_effect must be a valid ToolEffect") from exc
        if self.effect_argument is not None and not self.effect_argument.strip():
            raise ValueError("effect_argument must not be empty")
        if self.effect_mapping and self.effect_argument is None:
            raise ValueError("effect_argument is required when effect_mapping is provided")
        if self.effect_argument_default is not None:
            if self.effect_argument is None:
                raise ValueError(
                    "effect_argument is required when effect_argument_default is provided"
                )
            if not self.effect_argument_default.strip():
                raise ValueError("effect_argument_default must not be empty")
        effects: dict[str, ToolEffect] = {}
        for value, effect in self.effect_mapping.items():
            if not isinstance(value, str) or not value.strip():
                raise ValueError("effect_mapping keys must be non-empty strings")
            normalized = value.casefold()
            if normalized in effects:
                raise ValueError("effect_mapping keys must be unique ignoring case")
            try:
                effects[normalized] = ToolEffect(effect)
            except ValueError as exc:
                raise ValueError("effect_mapping values must be valid ToolEffect values") from exc
        object.__setattr__(self, "input_schema", MappingProxyType(dict(self.input_schema)))
        object.__setattr__(self, "default_effect", default_effect)
        object.__setattr__(self, "effect_mapping", MappingProxyType(effects))

    def effect_for(self, arguments: Mapping[str, object]) -> ToolEffect:
        """解析当前参数对应的最高影响等级。

        Args:
            arguments: 已完成稳定快照的工具参数。

        Returns:
            当前调用的影响等级；无法识别时返回保守默认值。
        """
        if self.effect_argument is None:
            return self.default_effect
        value = arguments.get(self.effect_argument, self.effect_argument_default)
        if not isinstance(value, str):
            return self.default_effect
        return self.effect_mapping.get(value.casefold(), self.default_effect)


@dataclass(frozen=True, slots=True)
class ToolContext:
    """向工具与权限策略传递最小调用上下文。

    Args:
        run_id: 当前 Loop 运行标识。
        step_id: 当前计划步骤标识。
        metadata: 由组合根提供的 JSON-compatible 关联数据。
        access_scope: 注册表必须先于业务授权器执行的强制访问范围。
    """

    run_id: str
    step_id: str | None = None
    metadata: Mapping[str, object] = field(default_factory=dict)
    access_scope: ToolAccessScope = ToolAccessScope.FULL

    def __post_init__(self) -> None:
        """递归复制并冻结元数据，避免授权前后的嵌套对象竞态。"""
        if not self.run_id.strip():
            raise ValueError("run_id must not be empty")
        try:
            access_scope = ToolAccessScope(self.access_scope)
        except ValueError as exc:
            raise ValueError("access_scope must be a valid ToolAccessScope") from exc
        frozen = _freeze_metadata_value(self.metadata, depth=0, active=set())
        if not isinstance(frozen, Mapping):
            raise ValueError("tool context metadata must be an object")
        object.__setattr__(self, "metadata", frozen)
        object.__setattr__(self, "access_scope", access_scope)


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


def _freeze_metadata_value(value: object, *, depth: int, active: set[int]) -> object:
    """递归快照 JSON 兼容上下文元数据。"""
    if depth > _MAX_METADATA_DEPTH:
        raise ValueError("tool context metadata exceeds maximum nesting depth")
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if not isfinite(value):
            raise ValueError("tool context metadata must not contain non-finite numbers")
        return value
    if isinstance(value, Mapping):
        identity = id(value)
        if identity in active:
            raise ValueError("tool context metadata must not contain cycles")
        active.add(identity)
        try:
            frozen: dict[str, object] = {}
            for key, item in value.items():
                if not isinstance(key, str):
                    raise ValueError("tool context metadata keys must be strings")
                frozen[key] = _freeze_metadata_value(
                    item,
                    depth=depth + 1,
                    active=active,
                )
            return MappingProxyType(frozen)
        finally:
            active.remove(identity)
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        identity = id(value)
        if identity in active:
            raise ValueError("tool context metadata must not contain cycles")
        active.add(identity)
        try:
            return tuple(
                _freeze_metadata_value(item, depth=depth + 1, active=active) for item in value
            )
        finally:
            active.remove(identity)
    raise ValueError("tool context metadata must contain only JSON-compatible values")
