"""来源无关的工具能力契约。"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import Enum
from types import MappingProxyType
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from matterloop_tools.base import Tool, ToolSpec

MCP_CAPABILITY_ANNOTATION = "matterloop/capability"


class ToolOrigin(str, Enum):
    """工具定义的受控来源。"""

    LOCAL = "local"
    LOCAL_MCP = "local_mcp"
    DATABASE_MCP = "database_mcp"
    SKILL = "skill"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class CapabilitySpec:
    """描述工具提供的稳定业务能力，不绑定具体工具名。

    Args:
        capability_id: 稳定能力标识，例如 ``polymer.job.submit``。
        operations: 工具支持的通用操作，例如 ``submit`` 或 ``query_status``。
        entities: 工具处理的业务对象，例如 ``polymer_job``。
        result_kind: 成功结果类型，例如 ``job_receipt``。
        required_result_fields: 完成证据必须存在且非空的字段路径。
        truthy_result_fields: 完成证据必须严格为真值的字段路径。
    """

    capability_id: str
    operations: tuple[str, ...] = ()
    entities: tuple[str, ...] = ()
    result_kind: str | None = None
    required_result_fields: tuple[str, ...] = ()
    truthy_result_fields: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        capability_id = self.capability_id.strip()
        if not capability_id:
            raise ValueError("capability_id must not be empty")
        object.__setattr__(self, "capability_id", capability_id)
        object.__setattr__(self, "operations", _normalized_values(self.operations, "operations"))
        object.__setattr__(self, "entities", _normalized_values(self.entities, "entities"))
        object.__setattr__(
            self,
            "required_result_fields",
            _normalized_values(self.required_result_fields, "required_result_fields"),
        )
        object.__setattr__(
            self,
            "truthy_result_fields",
            _normalized_values(self.truthy_result_fields, "truthy_result_fields"),
        )
        if self.result_kind is not None:
            result_kind = self.result_kind.strip()
            if not result_kind:
                raise ValueError("result_kind must not be empty when provided")
            object.__setattr__(self, "result_kind", result_kind)

    def matches(
        self,
        *,
        capability_id: str | None = None,
        operation: str | None = None,
        entity: str | None = None,
    ) -> bool:
        """判断能力是否满足请求中的确定性约束。"""
        if capability_id and self.capability_id != capability_id.strip():
            return False
        if operation and operation.strip().casefold() not in self.operations:
            return False
        return not entity or entity.strip().casefold() in self.entities

    def evidence_satisfied(self, result: Mapping[str, object]) -> bool:
        """根据结构化结果判断声明的完成证据是否满足。"""
        for path in self.required_result_fields:
            value, present = _resolve_path(result, path)
            if not present or value in (None, "", (), [], {}):
                return False
        for path in self.truthy_result_fields:
            value, present = _resolve_path(result, path)
            if not present or value is not True:
                return False
        return True

    def to_mapping(self) -> Mapping[str, object]:
        """转换为可写入 MCP annotations 或数据库的只读映射。"""
        payload: dict[str, object] = {
            "capability_id": self.capability_id,
            "operations": list(self.operations),
            "entities": list(self.entities),
            "required_result_fields": list(self.required_result_fields),
            "truthy_result_fields": list(self.truthy_result_fields),
        }
        if self.result_kind is not None:
            payload["result_kind"] = self.result_kind
        return MappingProxyType(payload)

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> CapabilitySpec:
        """从 MCP annotations 或数据库快照恢复能力契约。"""
        return cls(
            capability_id=str(value.get("capability_id") or ""),
            operations=_string_tuple(value.get("operations")),
            entities=_string_tuple(value.get("entities")),
            result_kind=_optional_string(value.get("result_kind")),
            required_result_fields=_string_tuple(value.get("required_result_fields")),
            truthy_result_fields=_string_tuple(value.get("truthy_result_fields")),
        )


@dataclass(frozen=True, slots=True)
class ToolDescriptor:
    """工具发现信息、能力语义和来源的统一运行时描述。"""

    tool_ref: str
    origin: ToolOrigin
    spec: ToolSpec
    capability: CapabilitySpec | None = None
    output_schema: Mapping[str, object] | None = None
    annotations: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        tool_ref = self.tool_ref.strip()
        if not tool_ref:
            raise ValueError("tool_ref must not be empty")
        object.__setattr__(self, "tool_ref", tool_ref)
        object.__setattr__(self, "origin", ToolOrigin(self.origin))
        if self.output_schema is not None:
            object.__setattr__(self, "output_schema", MappingProxyType(dict(self.output_schema)))
        object.__setattr__(self, "annotations", MappingProxyType(dict(self.annotations)))


def capability_from_annotations(
    annotations: Mapping[str, object] | None,
) -> CapabilitySpec | None:
    """读取 MatterLoop 命名空间下的 MCP 能力声明。"""
    if not isinstance(annotations, Mapping):
        return None
    raw = annotations.get(MCP_CAPABILITY_ANNOTATION)
    if not isinstance(raw, Mapping):
        return None
    try:
        return CapabilitySpec.from_mapping(raw)
    except (TypeError, ValueError):
        return None


def descriptor_for(tool: Tool) -> ToolDescriptor:
    """获取工具描述；旧工具没有 descriptor 时生成兼容的本地描述。"""
    descriptor = getattr(tool, "descriptor", None)
    if isinstance(descriptor, ToolDescriptor):
        return descriptor
    return ToolDescriptor(
        tool_ref=f"local:{tool.spec.name}",
        origin=ToolOrigin.LOCAL,
        spec=tool.spec,
    )


def _normalized_values(values: tuple[str, ...], field_name: str) -> tuple[str, ...]:
    normalized: list[str] = []
    seen: set[str] = set()
    for value in values:
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"{field_name} must contain non-empty strings")
        item = value.strip().casefold()
        if item not in seen:
            normalized.append(item)
            seen.add(item)
    return tuple(normalized)


def _string_tuple(value: object) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        return (value,)
    if not isinstance(value, (list, tuple)):
        raise ValueError("capability list fields must be strings or arrays")
    if not all(isinstance(item, str) for item in value):
        raise ValueError("capability list fields must contain strings")
    return tuple(value)


def _optional_string(value: object) -> str | None:
    if value is None:
        return None
    return str(value)


def _resolve_path(value: Mapping[str, object], path: str) -> tuple[object, bool]:
    current: object = value
    for segment in path.split("."):
        if not isinstance(current, Mapping) or segment not in current:
            return None, False
        current = current[segment]
    return current, True
