"""适配 Mapping 或属性对象的默认 MCP 响应映射器。"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from matterloop_tools.mcp.errors import McpProtocolError, McpRemoteError
from matterloop_tools.mcp.models import (
    McpCallResult,
    McpContent,
    McpContentKind,
    McpPromptArgument,
    McpPromptDefinition,
    McpPromptMessage,
    McpPromptPage,
    McpPromptResult,
    McpResourceDefinition,
    McpResourcePage,
    McpResourceResult,
    McpResourceTemplateDefinition,
    McpResourceTemplatePage,
    McpServerCapabilities,
    McpToolDefinition,
    McpToolPage,
)

_MISSING = object()


class StructuralMcpResponseMapper:
    """按 MCP 通用字段名读取原始 SDK 响应。

    支持字典、dataclass、Pydantic 对象及普通属性对象。供应商扩展若改变字段语义，调用方
    应注入自己的 ``McpResponseMapper``，避免业务代码直接依赖不稳定 SDK 类型。
    """

    def map_capabilities(self, payload: object) -> McpServerCapabilities:
        """映射初始化结果中的服务端能力。

        自定义 Session 若没有返回 capabilities 字段，则各能力保持 ``None``，连接继续采用
        兼容模式；字段存在时，未声明的能力会被明确标记为 ``False``。
        """
        capabilities = self._read(payload, ("capabilities",), _MISSING)
        if capabilities is _MISSING or capabilities is None:
            return McpServerCapabilities()
        return McpServerCapabilities(
            tools=self._capability_state(capabilities, "tools"),
            resources=self._capability_state(capabilities, "resources"),
            prompts=self._capability_state(capabilities, "prompts"),
            completions=self._capability_state(capabilities, "completions"),
            logging=self._capability_state(capabilities, "logging"),
        )

    def map_tool_page(self, payload: object) -> McpToolPage:
        """映射工具列表页。

        Args:
            payload: Session Adapter 返回的原始工具列表响应。

        Returns:
            稳定的工具列表页。

        Raises:
            McpProtocolError: 必需字段缺失或字段类型错误。
            McpRemoteError: 响应包含协议级错误。
        """
        operation = "list_tools"
        self._raise_remote_error(payload, operation)
        items = tuple(
            self._map_tool(item, operation)
            for item in self._sequence(self._required(payload, ("tools",), operation), operation)
        )
        return McpToolPage(items, self._cursor(payload, operation))

    def map_call_result(self, payload: object) -> McpCallResult:
        """映射工具调用结果。"""
        operation = "call_tool"
        self._raise_remote_error(payload, operation)
        raw_content = self._read(payload, ("content",), ())
        content = tuple(
            self._map_content(item, operation) for item in self._content_sequence(raw_content)
        )
        structured = self._optional_mapping(
            self._read(payload, ("structuredContent", "structured_content"), {}),
            operation,
            "structured content",
        )
        is_error = self._boolean(self._read(payload, ("isError", "is_error"), False), operation)
        metadata = self._optional_mapping(
            self._read(payload, ("_meta", "meta", "metadata"), {}),
            operation,
            "metadata",
        )
        return McpCallResult(content, structured, is_error, metadata)

    def map_resource_page(self, payload: object) -> McpResourcePage:
        """映射资源列表页。"""
        operation = "list_resources"
        self._raise_remote_error(payload, operation)
        items = tuple(
            self._map_resource(item, operation)
            for item in self._sequence(
                self._required(payload, ("resources",), operation), operation
            )
        )
        return McpResourcePage(items, self._cursor(payload, operation))

    def map_resource_result(self, payload: object) -> McpResourceResult:
        """映射资源读取结果。"""
        operation = "read_resource"
        self._raise_remote_error(payload, operation)
        contents = tuple(
            self._map_content(item, operation)
            for item in self._sequence(
                self._required(payload, ("contents", "content"), operation), operation
            )
        )
        metadata = self._optional_mapping(
            self._read(payload, ("_meta", "meta", "metadata"), {}),
            operation,
            "metadata",
        )
        return McpResourceResult(contents, metadata)

    def map_resource_template_page(self, payload: object) -> McpResourceTemplatePage:
        """映射参数化资源模板列表页。"""
        operation = "list_resource_templates"
        self._raise_remote_error(payload, operation)
        items = tuple(
            self._map_resource_template(item, operation)
            for item in self._sequence(
                self._required(
                    payload,
                    ("resourceTemplates", "resource_templates"),
                    operation,
                ),
                operation,
            )
        )
        return McpResourceTemplatePage(items, self._cursor(payload, operation))

    def map_prompt_page(self, payload: object) -> McpPromptPage:
        """映射 Prompt 列表页。"""
        operation = "list_prompts"
        self._raise_remote_error(payload, operation)
        items = tuple(
            self._map_prompt(item, operation)
            for item in self._sequence(self._required(payload, ("prompts",), operation), operation)
        )
        return McpPromptPage(items, self._cursor(payload, operation))

    def map_prompt_result(self, payload: object) -> McpPromptResult:
        """映射 Prompt 获取结果。"""
        operation = "get_prompt"
        self._raise_remote_error(payload, operation)
        messages = tuple(
            self._map_prompt_message(item, operation)
            for item in self._sequence(self._required(payload, ("messages",), operation), operation)
        )
        description = self._string_or_empty(
            self._read(payload, ("description",), ""), operation, "description"
        )
        metadata = self._optional_mapping(
            self._read(payload, ("_meta", "meta", "metadata"), {}),
            operation,
            "metadata",
        )
        return McpPromptResult(messages, description, metadata)

    def _map_tool(self, payload: object, operation: str) -> McpToolDefinition:
        name = self._non_empty_string(
            self._required(payload, ("name",), operation), operation, "name"
        )
        description = self._string_or_empty(
            self._read(payload, ("description",), ""), operation, "description"
        )
        input_schema = self._mapping(
            self._read(payload, ("inputSchema", "input_schema"), {"type": "object"}),
            operation,
            "input schema",
        )
        raw_output = self._read(payload, ("outputSchema", "output_schema"), None)
        output_schema = (
            None if raw_output is None else self._mapping(raw_output, operation, "output schema")
        )
        annotations = self._optional_mapping(
            self._read(payload, ("annotations",), {}), operation, "annotations"
        )
        return McpToolDefinition(name, description, input_schema, output_schema, annotations)

    def _map_resource(self, payload: object, operation: str) -> McpResourceDefinition:
        uri = self._uri_string(self._required(payload, ("uri",), operation), operation, "uri")
        name = self._non_empty_string(
            self._read(payload, ("name",), uri), operation, "resource name"
        )
        description = self._string_or_empty(
            self._read(payload, ("description",), ""), operation, "description"
        )
        mime_type = self._optional_string(
            self._read(payload, ("mimeType", "mime_type"), None), operation, "mime type"
        )
        size = self._optional_nonnegative_integer(
            self._read(payload, ("size",), None), operation, "size"
        )
        metadata = self._optional_mapping(
            self._read(payload, ("_meta", "meta", "metadata"), {}),
            operation,
            "metadata",
        )
        return McpResourceDefinition(uri, name, description, mime_type, size, metadata)

    def _map_resource_template(
        self,
        payload: object,
        operation: str,
    ) -> McpResourceTemplateDefinition:
        uri_template = self._non_empty_string(
            self._required(payload, ("uriTemplate", "uri_template"), operation),
            operation,
            "uri template",
        )
        name = self._non_empty_string(
            self._read(payload, ("name",), uri_template), operation, "resource template name"
        )
        description = self._string_or_empty(
            self._read(payload, ("description",), ""), operation, "description"
        )
        mime_type = self._optional_string(
            self._read(payload, ("mimeType", "mime_type"), None), operation, "mime type"
        )
        metadata = self._optional_mapping(
            self._read(payload, ("_meta", "meta", "metadata"), {}),
            operation,
            "metadata",
        )
        return McpResourceTemplateDefinition(
            uri_template,
            name,
            description,
            mime_type,
            metadata,
        )

    def _map_prompt(self, payload: object, operation: str) -> McpPromptDefinition:
        name = self._non_empty_string(
            self._required(payload, ("name",), operation), operation, "name"
        )
        description = self._string_or_empty(
            self._read(payload, ("description",), ""), operation, "description"
        )
        arguments = tuple(
            self._map_prompt_argument(item, operation)
            for item in self._optional_sequence(self._read(payload, ("arguments",), ()), operation)
        )
        return McpPromptDefinition(name, description, arguments)

    def _map_prompt_argument(self, payload: object, operation: str) -> McpPromptArgument:
        name = self._non_empty_string(
            self._required(payload, ("name",), operation), operation, "name"
        )
        description = self._string_or_empty(
            self._read(payload, ("description",), ""), operation, "description"
        )
        required = self._optional_boolean(
            self._read(payload, ("required",), False), operation, default=False
        )
        return McpPromptArgument(name, description, required)

    def _map_prompt_message(self, payload: object, operation: str) -> McpPromptMessage:
        role = self._non_empty_string(
            self._required(payload, ("role",), operation), operation, "role"
        )
        raw_content = self._required(payload, ("content",), operation)
        content = tuple(
            self._map_content(item, operation) for item in self._content_sequence(raw_content)
        )
        return McpPromptMessage(role, content)

    def _map_content(self, payload: object, operation: str) -> McpContent:
        if isinstance(payload, McpContent):
            return payload
        kind_value = self._read(payload, ("type", "kind"), _MISSING)
        if kind_value is _MISSING and self._looks_like_resource_contents(payload):
            kind_text = "resource"
        else:
            kind_text = self._string(
                "unknown" if kind_value is _MISSING else kind_value,
                operation,
                "content type",
            ).lower()
        if kind_text == "text":
            return McpContent(
                McpContentKind.TEXT,
                text=self._string(
                    self._required(payload, ("text",), operation), operation, "content text"
                ),
                metadata=self._content_metadata(payload, operation),
            )
        if kind_text in {"image", "audio", "binary", "blob"}:
            stable_kind = {
                "image": McpContentKind.IMAGE,
                "audio": McpContentKind.AUDIO,
                "binary": McpContentKind.BINARY,
                "blob": McpContentKind.BINARY,
            }[kind_text]
            return McpContent(
                stable_kind,
                data=self._read(payload, ("data", "blob"), None),
                mime_type=self._optional_string(
                    self._read(payload, ("mimeType", "mime_type"), None),
                    operation,
                    "mime type",
                ),
                metadata=self._content_metadata(payload, operation),
            )
        if kind_text in {"resource", "resource_link"}:
            resource = self._read(payload, ("resource",), payload)
            return McpContent(
                McpContentKind.RESOURCE,
                text=self._optional_string(
                    self._read(resource, ("text",), None), operation, "resource text"
                ),
                data=self._read(resource, ("blob", "data"), None),
                mime_type=self._optional_string(
                    self._read(resource, ("mimeType", "mime_type"), None),
                    operation,
                    "mime type",
                ),
                uri=self._optional_uri_string(
                    self._read(resource, ("uri",), None), operation, "resource uri"
                ),
                metadata=self._content_metadata(payload, operation),
            )
        if kind_text in {"json", "structured"}:
            return McpContent(
                McpContentKind.JSON,
                data=self._read(payload, ("data", "json", "value"), None),
                metadata=self._content_metadata(payload, operation),
            )
        return McpContent(
            McpContentKind.UNKNOWN,
            metadata={"remote_type": kind_text, **self._content_metadata(payload, operation)},
        )

    def _content_metadata(self, payload: object, operation: str) -> Mapping[str, object]:
        value = self._read_non_null(
            payload,
            ("_meta", "meta", "metadata", "annotations"),
            {},
        )
        return self._optional_mapping(
            value,
            operation,
            "content metadata",
        )

    def _looks_like_resource_contents(self, payload: object) -> bool:
        uri = self._read(payload, ("uri",), _MISSING)
        text = self._read(payload, ("text",), _MISSING)
        blob = self._read(payload, ("blob",), _MISSING)
        return uri is not _MISSING and (text is not _MISSING or blob is not _MISSING)

    def _cursor(self, payload: object, operation: str) -> str | None:
        return self._optional_string(
            self._read(payload, ("nextCursor", "next_cursor"), None), operation, "cursor"
        )

    def _capability_state(self, payload: object, name: str) -> bool:
        value = self._read(payload, (name,), _MISSING)
        if isinstance(value, bool):
            return value
        return value is not _MISSING and value is not None

    def _raise_remote_error(self, payload: object, operation: str) -> None:
        error = self._read(payload, ("error",), _MISSING)
        if error is _MISSING or error is None:
            return
        code = self._read(error, ("code",), None)
        # JSON-RPC 标准错误码是整数；远端字符串可能包含凭据或自由文本，不进入异常。
        safe_code: int | None = (
            code if isinstance(code, int) and not isinstance(code, bool) else None
        )
        raise McpRemoteError(operation, safe_code)

    @staticmethod
    def _read(payload: object, names: tuple[str, ...], default: object) -> object:
        if isinstance(payload, Mapping):
            for name in names:
                if name in payload:
                    return payload[name]
            return default
        for name in names:
            if hasattr(payload, name):
                return getattr(payload, name)
        return default

    @classmethod
    def _read_non_null(
        cls,
        payload: object,
        names: tuple[str, ...],
        default: object,
    ) -> object:
        """读取第一个存在且非空的别名字段。"""
        for name in names:
            value = cls._read(payload, (name,), _MISSING)
            if value is not _MISSING and value is not None:
                return value
        return default

    def _required(self, payload: object, names: tuple[str, ...], operation: str) -> object:
        value = self._read(payload, names, _MISSING)
        if value is _MISSING:
            raise McpProtocolError(operation, f"missing field {names[0]}")
        return value

    @staticmethod
    def _sequence(value: object, operation: str) -> Sequence[object]:
        if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
            return value
        raise McpProtocolError(operation, "expected an array")

    def _optional_sequence(self, value: object, operation: str) -> Sequence[object]:
        if value is None:
            return ()
        return self._sequence(value, operation)

    @staticmethod
    def _content_sequence(value: object) -> Sequence[object]:
        if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
            return value
        return (value,)

    @staticmethod
    def _mapping(value: object, operation: str, field_name: str) -> Mapping[str, object]:
        if isinstance(value, Mapping) and all(isinstance(key, str) for key in value):
            return {str(key): item for key, item in value.items()}
        model_dump = getattr(value, "model_dump", None)
        if callable(model_dump):
            try:
                dumped = model_dump(by_alias=True, exclude_none=True)
            except Exception:
                raise McpProtocolError(
                    operation, f"{field_name} could not be converted to an object"
                ) from None
            if isinstance(dumped, Mapping) and all(isinstance(key, str) for key in dumped):
                return {str(key): item for key, item in dumped.items()}
        if not isinstance(value, Mapping):
            raise McpProtocolError(operation, f"{field_name} must be an object")
        raise McpProtocolError(operation, f"{field_name} keys must be strings")

    def _optional_mapping(
        self,
        value: object,
        operation: str,
        field_name: str,
    ) -> Mapping[str, object]:
        if value is None:
            return {}
        return self._mapping(value, operation, field_name)

    @staticmethod
    def _string(value: object, operation: str, field_name: str) -> str:
        if not isinstance(value, str):
            raise McpProtocolError(operation, f"{field_name} must be a string")
        return value

    def _string_or_empty(self, value: object, operation: str, field_name: str) -> str:
        if value is None:
            return ""
        return self._string(value, operation, field_name)

    def _non_empty_string(self, value: object, operation: str, field_name: str) -> str:
        result = self._string(value, operation, field_name)
        if not result.strip():
            raise McpProtocolError(operation, f"{field_name} must not be empty")
        return result

    @staticmethod
    def _uri_string(value: object, operation: str, field_name: str) -> str:
        if value is None:
            raise McpProtocolError(operation, f"{field_name} must be a URI string")
        if isinstance(value, str):
            result = value
        else:
            try:
                result = str(value)
            except Exception:
                raise McpProtocolError(operation, f"{field_name} must be a URI string") from None
        if not result.strip():
            raise McpProtocolError(operation, f"{field_name} must not be empty")
        return result

    def _optional_uri_string(
        self,
        value: object,
        operation: str,
        field_name: str,
    ) -> str | None:
        if value is None:
            return None
        return self._uri_string(value, operation, field_name)

    def _optional_string(self, value: object, operation: str, field_name: str) -> str | None:
        if value is None:
            return None
        return self._string(value, operation, field_name)

    @staticmethod
    def _boolean(value: object, operation: str) -> bool:
        if not isinstance(value, bool):
            raise McpProtocolError(operation, "expected a boolean")
        return value

    def _optional_boolean(self, value: object, operation: str, *, default: bool) -> bool:
        if value is None:
            return default
        return self._boolean(value, operation)

    @staticmethod
    def _optional_integer(value: object, operation: str, field_name: str) -> int | None:
        if value is None:
            return None
        if not isinstance(value, int) or isinstance(value, bool):
            raise McpProtocolError(operation, f"{field_name} must be an integer")
        return value

    def _optional_nonnegative_integer(
        self,
        value: object,
        operation: str,
        field_name: str,
    ) -> int | None:
        result = self._optional_integer(value, operation, field_name)
        if result is not None and result < 0:
            raise McpProtocolError(operation, f"{field_name} must not be negative")
        return result
