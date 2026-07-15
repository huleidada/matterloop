"""供应商无关的 MCP 数据传输对象。"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import Enum
from math import isfinite
from types import MappingProxyType

from matterloop_tools.mcp.errors import McpConfigurationError


def _frozen_mapping(value: Mapping[str, object]) -> Mapping[str, object]:
    return MappingProxyType(dict(value))


class McpContentKind(str, Enum):
    """MCP 内容块的稳定分类。"""

    TEXT = "text"
    JSON = "json"
    IMAGE = "image"
    AUDIO = "audio"
    RESOURCE = "resource"
    BINARY = "binary"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class McpContent:
    """标准化的 MCP 内容块。

    Attributes:
        kind: 内容类型。
        text: 文本内容；非文本块可以为空。
        data: JSON 值、Base64 文本或其他已经由 Mapper 约束的值。
        mime_type: 可选 MIME 类型。
        uri: 资源内容对应的 URI。
        metadata: 不含凭据的扩展元数据。
    """

    kind: McpContentKind
    text: str | None = None
    data: object | None = None
    mime_type: str | None = None
    uri: str | None = None
    metadata: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "metadata", _frozen_mapping(self.metadata))


@dataclass(frozen=True, slots=True)
class McpToolDefinition:
    """MCP 服务声明的单个工具。"""

    name: str
    description: str = ""
    input_schema: Mapping[str, object] = field(default_factory=lambda: {"type": "object"})
    output_schema: Mapping[str, object] | None = None
    annotations: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise ValueError("MCP tool name must not be empty")
        object.__setattr__(self, "input_schema", _frozen_mapping(self.input_schema))
        if self.output_schema is not None:
            object.__setattr__(self, "output_schema", _frozen_mapping(self.output_schema))
        object.__setattr__(self, "annotations", _frozen_mapping(self.annotations))


@dataclass(frozen=True, slots=True)
class McpResourceDefinition:
    """MCP 服务声明的可读取资源。"""

    uri: str
    name: str
    description: str = ""
    mime_type: str | None = None
    size: int | None = None
    metadata: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.uri.strip() or not self.name.strip():
            raise ValueError("MCP resource uri and name must not be empty")
        if self.size is not None and self.size < 0:
            raise ValueError("MCP resource size must not be negative")
        object.__setattr__(self, "metadata", _frozen_mapping(self.metadata))


@dataclass(frozen=True, slots=True)
class McpResourceTemplateDefinition:
    """MCP 服务声明的参数化资源 URI 模板。"""

    uri_template: str
    name: str
    description: str = ""
    mime_type: str | None = None
    metadata: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.uri_template.strip() or not self.name.strip():
            raise ValueError("MCP resource template uri and name must not be empty")
        object.__setattr__(self, "metadata", _frozen_mapping(self.metadata))


@dataclass(frozen=True, slots=True)
class McpPromptArgument:
    """MCP Prompt 的一个输入参数定义。"""

    name: str
    description: str = ""
    required: bool = False

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise ValueError("MCP prompt argument name must not be empty")


@dataclass(frozen=True, slots=True)
class McpPromptDefinition:
    """MCP 服务声明的 Prompt 模板。"""

    name: str
    description: str = ""
    arguments: tuple[McpPromptArgument, ...] = ()

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise ValueError("MCP prompt name must not be empty")


@dataclass(frozen=True, slots=True)
class McpToolPage:
    """MCP 工具发现的一页结果。"""

    items: tuple[McpToolDefinition, ...]
    next_cursor: str | None = None


@dataclass(frozen=True, slots=True)
class McpResourcePage:
    """MCP 资源发现的一页结果。"""

    items: tuple[McpResourceDefinition, ...]
    next_cursor: str | None = None


@dataclass(frozen=True, slots=True)
class McpResourceTemplatePage:
    """MCP 资源模板发现的一页结果。"""

    items: tuple[McpResourceTemplateDefinition, ...]
    next_cursor: str | None = None


@dataclass(frozen=True, slots=True)
class McpPromptPage:
    """MCP Prompt 发现的一页结果。"""

    items: tuple[McpPromptDefinition, ...]
    next_cursor: str | None = None


@dataclass(frozen=True, slots=True)
class McpCallResult:
    """标准化的 MCP 工具调用结果。"""

    content: tuple[McpContent, ...] = ()
    structured_content: Mapping[str, object] = field(default_factory=dict)
    is_error: bool = False
    metadata: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "structured_content", _frozen_mapping(self.structured_content))
        object.__setattr__(self, "metadata", _frozen_mapping(self.metadata))


@dataclass(frozen=True, slots=True)
class McpResourceResult:
    """标准化的 MCP 资源读取结果。"""

    contents: tuple[McpContent, ...]
    metadata: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "metadata", _frozen_mapping(self.metadata))


@dataclass(frozen=True, slots=True)
class McpPromptMessage:
    """解析 Prompt 后返回的一条角色消息。"""

    role: str
    content: tuple[McpContent, ...]

    def __post_init__(self) -> None:
        if not self.role.strip():
            raise ValueError("MCP prompt message role must not be empty")


@dataclass(frozen=True, slots=True)
class McpPromptResult:
    """标准化的 MCP Prompt 获取结果。"""

    messages: tuple[McpPromptMessage, ...]
    description: str = ""
    metadata: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "metadata", _frozen_mapping(self.metadata))


@dataclass(frozen=True, slots=True)
class McpServerCapabilities:
    """初始化阶段协商得到的服务端能力。

    ``None`` 表示自定义 Session 没有提供能力信息；``False`` 表示初始化结果明确未声明该
    能力；``True`` 表示可以调用对应操作。
    """

    tools: bool | None = None
    resources: bool | None = None
    prompts: bool | None = None
    completions: bool | None = None
    logging: bool | None = None


@dataclass(frozen=True, slots=True)
class McpCatalog:
    """一次连接可发现的工具、资源与 Prompt 快照。"""

    tools: tuple[McpToolDefinition, ...]
    resources: tuple[McpResourceDefinition, ...]
    resource_templates: tuple[McpResourceTemplateDefinition, ...]
    prompts: tuple[McpPromptDefinition, ...]


@dataclass(frozen=True, slots=True)
class McpLimits:
    """MCP 本地资源硬限制。

    Attributes:
        request_timeout_seconds: 单次 Session 请求超时。
        initialize_timeout_seconds: 初始化协商超时。
        close_timeout_seconds: 关闭 Session 超时。
        max_pages: 单次完整发现最多读取的页数。
        max_items: 单类发现最多接收的条目数。
        max_content_blocks: 单次结果最多接受的内容块数量。
        max_result_characters: 转为 MatterLoop ToolResult 时的最大字符数。
    """

    request_timeout_seconds: float = 30.0
    initialize_timeout_seconds: float = 15.0
    close_timeout_seconds: float = 10.0
    max_pages: int = 20
    max_items: int = 1_000
    max_content_blocks: int = 256
    max_result_characters: int = 200_000

    def __post_init__(self) -> None:
        timeouts = (
            self.request_timeout_seconds,
            self.initialize_timeout_seconds,
            self.close_timeout_seconds,
        )
        integer_limits = (
            self.max_pages,
            self.max_items,
            self.max_content_blocks,
            self.max_result_characters,
        )
        if any(
            type(value) not in (int, float) or not isfinite(value) or value <= 0
            for value in timeouts
        ) or any(type(value) is not int or value <= 0 for value in integer_limits):
            raise McpConfigurationError("all MCP limits must be positive")


@dataclass(frozen=True, slots=True)
class McpServerConfig:
    """一个注入式 MCP Session 的生命周期配置。

    Attributes:
        name: 注册表中的唯一服务名称。
        tool_namespace: 暴露为 MatterLoop Tool 时使用的安全命名空间。
        limits: 本地超时、分页和结果上限。
        initialize_on_start: 注册时是否调用 Session 的 initialize。
        owns_session: 连接关闭时是否负责关闭注入的 Session。
    """

    name: str
    tool_namespace: str
    limits: McpLimits = field(default_factory=McpLimits)
    initialize_on_start: bool = True
    owns_session: bool = False

    def __post_init__(self) -> None:
        if (
            not isinstance(self.name, str)
            or not self.name.strip()
            or not isinstance(self.tool_namespace, str)
            or not self.tool_namespace.strip()
        ):
            raise McpConfigurationError("MCP server name and tool namespace must not be empty")
        if not isinstance(self.limits, McpLimits):
            raise McpConfigurationError("MCP server limits must be McpLimits")
        if type(self.initialize_on_start) is not bool or type(self.owns_session) is not bool:
            raise McpConfigurationError("MCP lifecycle flags must be booleans")
