"""定义模型能力、描述信息与能力需求。"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import Enum
from types import MappingProxyType


class CapabilityStatus(str, Enum):
    """表示一项模型能力的已知状态。"""

    UNKNOWN = "unknown"
    SUPPORTED = "supported"
    UNSUPPORTED = "unsupported"


class ModelFeature(str, Enum):
    """列出可供组合根和调度器查询的通用模型能力。"""

    TEXT_GENERATION = "text_generation"
    DEVELOPER_MESSAGES = "developer_messages"
    TOOL_CALLING = "tool_calling"
    PARALLEL_TOOL_CALLING = "parallel_tool_calling"
    NAMED_TOOL_CHOICE = "named_tool_choice"
    JSON_OBJECT_OUTPUT = "json_object_output"
    JSON_SCHEMA_OUTPUT = "json_schema_output"
    RESPONSE_ID_CONTINUATION = "response_id_continuation"
    OPAQUE_CONTINUATION = "opaque_continuation"
    REASONING = "reasoning"
    TEMPERATURE = "temperature"


@dataclass(frozen=True, slots=True)
class ModelCapabilities:
    """保存模型明确支持和明确不支持的能力。

    未出现在两个集合中的能力状态为 :attr:`CapabilityStatus.UNKNOWN`，
    不会被误判为不支持。

    Args:
        supported: 适配器或调用方已确认支持的能力。
        unsupported: 适配器或调用方已确认不支持的能力。
    """

    supported: frozenset[ModelFeature] = field(default_factory=frozenset)
    unsupported: frozenset[ModelFeature] = field(default_factory=frozenset)

    def __post_init__(self) -> None:
        """规范集合并拒绝相互矛盾的能力声明。"""
        supported = frozenset(self.supported)
        unsupported = frozenset(self.unsupported)
        if any(not isinstance(feature, ModelFeature) for feature in (*supported, *unsupported)):
            raise TypeError("model capabilities must contain ModelFeature values")
        overlap = supported & unsupported
        if overlap:
            names = ", ".join(sorted(feature.value for feature in overlap))
            raise ValueError(f"model capabilities contain conflicting features: {names}")
        object.__setattr__(self, "supported", supported)
        object.__setattr__(self, "unsupported", unsupported)

    def status(self, feature: ModelFeature) -> CapabilityStatus:
        """返回指定能力的三态结果。

        Args:
            feature: 需要查询的通用能力。

        Returns:
            明确支持、明确不支持或未知。
        """
        if feature in self.supported:
            return CapabilityStatus.SUPPORTED
        if feature in self.unsupported:
            return CapabilityStatus.UNSUPPORTED
        return CapabilityStatus.UNKNOWN

    def supports(self, feature: ModelFeature) -> bool:
        """返回指定能力是否已被明确标记为支持。"""
        return self.status(feature) is CapabilityStatus.SUPPORTED


@dataclass(frozen=True, slots=True)
class ModelDescriptor:
    """描述一个已构造模型客户端的非敏感信息。

    Args:
        provider: 稳定的供应商或自定义实现标识。
        model: 客户端实际使用的模型标识。
        capabilities: 已知能力集。
        metadata: 不含凭据的非敏感扩展元数据。
    """

    provider: str
    model: str
    capabilities: ModelCapabilities = field(default_factory=ModelCapabilities)
    metadata: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        """规范标识并复制顶层元数据。"""
        provider = self.provider.strip()
        model = self.model.strip()
        if not provider:
            raise ValueError("model descriptor provider must not be empty")
        if not model:
            raise ValueError("model descriptor model must not be empty")
        if not isinstance(self.capabilities, ModelCapabilities):
            raise TypeError("model descriptor capabilities must be ModelCapabilities")
        object.__setattr__(self, "provider", provider)
        object.__setattr__(self, "model", model)
        object.__setattr__(self, "metadata", MappingProxyType(dict(self.metadata)))


@dataclass(frozen=True, slots=True)
class ModelRequirements:
    """定义组合根选择模型时需要满足的条件。

    Args:
        required_features: 必须支持的能力。
        provider: 可选的供应商精确限定。
        model: 可选的模型标识精确限定。
        allow_unknown: 是否允许未知能力通过匹配；默认快速失败。
    """

    required_features: frozenset[ModelFeature] = field(default_factory=frozenset)
    provider: str | None = None
    model: str | None = None
    allow_unknown: bool = False

    def __post_init__(self) -> None:
        """规范限定条件并校验能力类型。"""
        required = frozenset(self.required_features)
        if any(not isinstance(feature, ModelFeature) for feature in required):
            raise TypeError("model requirements must contain ModelFeature values")
        provider = None if self.provider is None else self.provider.strip()
        model = None if self.model is None else self.model.strip()
        if self.provider is not None and not provider:
            raise ValueError("model requirements provider must not be empty")
        if self.model is not None and not model:
            raise ValueError("model requirements model must not be empty")
        object.__setattr__(self, "required_features", required)
        object.__setattr__(self, "provider", provider)
        object.__setattr__(self, "model", model)

    def matches(self, descriptor: ModelDescriptor) -> bool:
        """判断模型描述是否满足当前需求。

        Args:
            descriptor: 候选模型的非敏感描述。

        Returns:
            供应商、模型标识和所有必需能力是否匹配。
        """
        if self.provider is not None and descriptor.provider != self.provider:
            return False
        if self.model is not None and descriptor.model != self.model:
            return False
        for feature in self.required_features:
            status = descriptor.capabilities.status(feature)
            if status is CapabilityStatus.UNSUPPORTED:
                return False
            if status is CapabilityStatus.UNKNOWN and not self.allow_unknown:
                return False
        return True


__all__ = [
    "CapabilityStatus",
    "ModelCapabilities",
    "ModelDescriptor",
    "ModelFeature",
    "ModelRequirements",
]
