"""提供可配置的 OpenAI-compatible Chat Completions 通用适配器。"""

from __future__ import annotations

import copy
import json
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from enum import Enum
from types import MappingProxyType
from typing import Protocol, cast, runtime_checkable

from matterloop_models.base import (
    MessageRole,
    ModelRequest,
    ModelResponse,
    TokenUsage,
    ToolCall,
)
from matterloop_models.capabilities import ModelCapabilities, ModelDescriptor, ModelFeature
from matterloop_models.errors import (
    ModelAuthenticationError,
    ModelCapabilityError,
    ModelInvocationError,
    ModelPaymentRequiredError,
    ModelRateLimitError,
    ModelResponseParseError,
    ModelServiceError,
)

_MANAGED_PARAMETERS = frozenset(
    {
        "model",
        "messages",
        "tools",
        "tool_choice",
        "response_format",
        "max_tokens",
        "max_completion_tokens",
        "temperature",
        "stream",
    }
)
_CLIENT_ONLY_PARAMETERS = frozenset(
    {"api_key", "authorization", "base_url", "default_headers", "organization", "project"}
)


def _copy_mapping(value: Mapping[str, object]) -> dict[str, object]:
    """深复制普通映射，隔离调用方随后对嵌套参数的修改。"""
    return cast(dict[str, object], copy.deepcopy(dict(value)))


class ChatDeveloperRole(str, Enum):
    """指定通用 developer 消息在目标 Chat API 中的角色。"""

    SYSTEM = "system"
    DEVELOPER = "developer"


class ChatStructuredOutputMode(str, Enum):
    """描述目标 Chat API 对结构化输出的原生支持级别。"""

    JSON_OBJECT = "json_object"
    JSON_SCHEMA = "json_schema"
    PROMPT_ONLY = "prompt_only"


class ChatMaxTokensField(str, Enum):
    """指定目标 Chat API 使用的输出 Token 上限字段。"""

    MAX_TOKENS = "max_tokens"
    MAX_COMPLETION_TOKENS = "max_completion_tokens"


class OpenAICompatibleChatCompletionsResource(Protocol):
    """OpenAI-compatible 客户端需要暴露的 completions 资源。"""

    @property
    def create(self) -> Callable[..., Awaitable[object]]:
        """返回异步 Chat Completions 创建函数。"""
        ...


class OpenAICompatibleChatResource(Protocol):
    """OpenAI-compatible 客户端需要暴露的 chat 资源。"""

    @property
    def completions(self) -> OpenAICompatibleChatCompletionsResource:
        """返回 Chat Completions 资源。"""
        ...


class OpenAICompatibleChatClient(Protocol):
    """通用 Chat 适配器依赖的最小异步客户端协议。"""

    @property
    def chat(self) -> OpenAICompatibleChatResource:
        """返回 Chat API 资源。"""
        ...


@runtime_checkable
class SupportsAsyncClose(Protocol):
    """描述可以由适配器接管关闭责任的异步客户端。"""

    async def close(self) -> None:
        """关闭客户端持有的连接资源。"""
        ...


@dataclass(frozen=True, slots=True)
class OpenAICompatibleChatConfig:
    """配置一个遵循 OpenAI Chat Completions 形状的供应商。

    Args:
        provider: 稳定供应商标识，用于安全元数据和 continuation 亲和校验。
        model: 调用方明确选择的模型标识。
        developer_role: 目标 API 如何接收 MatterLoop developer 消息。
        structured_output_mode: 目标 API 支持的结构化输出模式。
        max_tokens_field: 目标 API 使用的输出 Token 上限字段。
        enable_strict_tools: 是否向供应商发送工具 ``strict`` 标志。
        preserve_reasoning_content: 是否仅在私有工具 continuation 中保留推理内容。
        extra_parameters: 额外的非敏感请求参数；不能覆盖适配器管理字段。

    Notes:
        配置不保存 API Key、base URL、请求头或组织信息。端点、凭据、代理、重试和
        连接池全部属于调用方构造注入客户端的职责。
    """

    provider: str
    model: str
    developer_role: ChatDeveloperRole = ChatDeveloperRole.SYSTEM
    structured_output_mode: ChatStructuredOutputMode = ChatStructuredOutputMode.JSON_OBJECT
    max_tokens_field: ChatMaxTokensField = ChatMaxTokensField.MAX_TOKENS
    enable_strict_tools: bool = False
    preserve_reasoning_content: bool = False
    extra_parameters: Mapping[str, object] = field(
        default_factory=dict,
        repr=False,
        compare=False,
    )

    def __post_init__(self) -> None:
        """校验供应商配置并复制额外请求参数。"""
        provider = self.provider.strip()
        model = self.model.strip()
        if not provider or "\n" in provider or "\r" in provider:
            raise ValueError("provider must be non-empty single-line text")
        if not model or "\n" in model or "\r" in model:
            raise ValueError("model must be non-empty single-line text")
        if not isinstance(self.developer_role, ChatDeveloperRole):
            raise TypeError("developer role must be a ChatDeveloperRole")
        if not isinstance(self.structured_output_mode, ChatStructuredOutputMode):
            raise TypeError("structured output mode must be a ChatStructuredOutputMode")
        if not isinstance(self.max_tokens_field, ChatMaxTokensField):
            raise TypeError("max tokens field must be a ChatMaxTokensField")
        parameters = _copy_mapping(self.extra_parameters)
        normalized_keys = {key.lower() for key in parameters}
        conflicts = normalized_keys & (_MANAGED_PARAMETERS | _CLIENT_ONLY_PARAMETERS)
        if conflicts:
            names = ", ".join(sorted(conflicts))
            raise ValueError(f"extra parameters contain managed or client-only fields: {names}")
        object.__setattr__(self, "provider", provider)
        object.__setattr__(self, "model", model)
        object.__setattr__(self, "extra_parameters", MappingProxyType(parameters))


class ChatCompletionsContinuation:
    """保存一次 Chat 工具事务所需的私有消息历史。

    完整历史可能包含 ``reasoning_content``，因此不提供公开历史访问器，且 ``repr``
    只展示非敏感供应商和模型标识。continuation 还绑定创建它的适配器实例，防止热替换
    后把私有历史误发到另一租户或端点。
    """

    __slots__ = (
        "_expected_tool_call_ids",
        "_messages",
        "_model",
        "_owner",
        "_provider",
    )

    def __init__(
        self,
        *,
        provider: str,
        model: str,
        owner: object,
        messages: Sequence[Mapping[str, object]],
        expected_tool_call_ids: Sequence[str],
    ) -> None:
        self._provider = provider
        self._model = model
        self._owner = owner
        self._messages = tuple(_copy_mapping(message) for message in messages)
        self._expected_tool_call_ids = tuple(expected_tool_call_ids)

    @property
    def provider(self) -> str:
        """返回创建该续轮状态的供应商适配器标识。"""
        return f"openai_compatible_chat:{self._provider}"

    @property
    def model(self) -> str:
        """返回创建续轮状态的非敏感模型标识。"""
        return self._model

    def __repr__(self) -> str:
        """隐藏对话、推理内容、工具参数和适配器亲和标识。"""
        return f"ChatCompletionsContinuation(provider={self.provider!r}, model={self.model!r})"

    def _matches(self, owner: object, provider: str, model: str) -> bool:
        return self._owner is owner and self._provider == provider and self._model == model

    def _copy_messages(self) -> list[dict[str, object]]:
        return [_copy_mapping(message) for message in self._messages]

    def _validate_tool_outputs(self, request: ModelRequest) -> None:
        provided = tuple(output.call_id for output in request.tool_outputs)
        if len(set(provided)) != len(provided):
            raise ValueError("tool outputs must not contain duplicate call ids")
        if set(provided) != set(self._expected_tool_call_ids):
            raise ValueError("tool outputs must match every pending tool call exactly")


class OpenAICompatibleChatModelClient:
    """把可配置的 OpenAI-compatible Chat API 适配为 MatterLoop 模型协议。

    Args:
        config: 供应商能力和非敏感请求配置。
        client: 调用方创建的异步 Chat 客户端。
        owns_client: 是否把客户端关闭责任转移给适配器。

    Raises:
        TypeError: 请求接管关闭责任，但客户端不支持异步 ``close``。
    """

    def __init__(
        self,
        config: OpenAICompatibleChatConfig,
        *,
        client: OpenAICompatibleChatClient,
        owns_client: bool = False,
    ) -> None:
        if owns_client and not isinstance(client, SupportsAsyncClose):
            raise TypeError("owned compatible chat client must provide async close()")
        self._config = config
        self._client = client
        self._closer = cast(SupportsAsyncClose, client) if owns_client else None
        self._owner = object()

    @property
    def descriptor(self) -> ModelDescriptor:
        """返回可用于注册和能力选择的非敏感模型描述。"""
        supported = {
            ModelFeature.TEXT_GENERATION,
            ModelFeature.TOOL_CALLING,
            ModelFeature.OPAQUE_CONTINUATION,
            ModelFeature.TEMPERATURE,
        }
        unsupported = {ModelFeature.RESPONSE_ID_CONTINUATION}
        mode = self._config.structured_output_mode
        if mode in {
            ChatStructuredOutputMode.JSON_OBJECT,
            ChatStructuredOutputMode.JSON_SCHEMA,
        }:
            supported.add(ModelFeature.JSON_OBJECT_OUTPUT)
        else:
            unsupported.add(ModelFeature.JSON_OBJECT_OUTPUT)
        if mode is ChatStructuredOutputMode.JSON_SCHEMA:
            supported.add(ModelFeature.JSON_SCHEMA_OUTPUT)
        else:
            unsupported.add(ModelFeature.JSON_SCHEMA_OUTPUT)
        if self._config.developer_role is ChatDeveloperRole.DEVELOPER:
            supported.add(ModelFeature.DEVELOPER_MESSAGES)
        if self._config.preserve_reasoning_content:
            supported.add(ModelFeature.REASONING)
        return ModelDescriptor(
            provider=self._config.provider,
            model=self._config.model,
            capabilities=ModelCapabilities(
                supported=frozenset(supported),
                unsupported=frozenset(unsupported),
            ),
            metadata={
                "api": "chat_completions",
                "structured_output_mode": mode.value,
            },
        )

    async def generate(self, request: ModelRequest) -> ModelResponse:
        """调用 Chat Completions 并归一化文本、工具、用量与私有续轮状态。

        Args:
            request: 与供应商无关的模型请求。

        Returns:
            不包含 SDK 原始对象、凭据或公开推理内容的模型响应。

        Raises:
            ModelCapabilityError: 请求使用目标配置不支持的能力。
            ModelAuthenticationError: 供应商返回 401 或 403。
            ModelPaymentRequiredError: 供应商返回 402。
            ModelRateLimitError: 供应商返回 429。
            ModelServiceError: 供应商返回 5xx。
            ModelInvocationError: 供应商调用因其他原因失败。
            ModelResponseParseError: 响应无法安全归一化。
        """
        self._validate_request(request)
        parameters, history = self._build_parameters(request)
        try:
            response = await self._client.chat.completions.create(**parameters)
        except Exception as exc:
            raise self._safe_invocation_error(exc) from None
        try:
            return self._parse_response(response, history)
        except ModelResponseParseError as exc:
            if exc.usage is None:
                exc.usage = self._parse_usage(response)
            raise

    async def aclose(self) -> None:
        """仅在显式取得所有权时关闭调用方注入的客户端。"""
        if self._closer is not None:
            await self._closer.close()

    def _validate_request(self, request: ModelRequest) -> None:
        if request.previous_response_id is not None and request.continuation is None:
            raise ModelCapabilityError(
                f"{self._config.provider} Chat Completions requires an opaque continuation"
            )

    def _provider_parameters(self, request: ModelRequest) -> Mapping[str, object]:
        del request
        return self._config.extra_parameters

    def _structured_output_mode(self, request: ModelRequest) -> ChatStructuredOutputMode:
        del request
        return self._config.structured_output_mode

    def _build_parameters(
        self,
        request: ModelRequest,
    ) -> tuple[dict[str, object], list[dict[str, object]]]:
        messages = self._build_messages(request)
        parameters = _copy_mapping(self._provider_parameters(request))
        conflicts = set(parameters) & _MANAGED_PARAMETERS
        if conflicts:
            names = ", ".join(sorted(conflicts))
            raise ValueError(f"provider parameters override managed fields: {names}")
        parameters.update({"model": self._config.model, "messages": messages})
        if request.tools:
            parameters["tools"] = [
                {
                    "type": "function",
                    "function": {
                        "name": tool.name,
                        "description": tool.description,
                        "parameters": _copy_mapping(tool.parameters),
                        **(
                            {"strict": True}
                            if self._config.enable_strict_tools and tool.strict
                            else {}
                        ),
                    },
                }
                for tool in request.tools
            ]
        if request.tool_choice is not None:
            parameters["tool_choice"] = request.tool_choice.value
        if request.response_schema is not None:
            mode = self._structured_output_mode(request)
            if mode is ChatStructuredOutputMode.JSON_OBJECT:
                parameters["response_format"] = {"type": "json_object"}
            elif mode is ChatStructuredOutputMode.JSON_SCHEMA:
                parameters["response_format"] = {
                    "type": "json_schema",
                    "json_schema": {
                        "name": request.response_schema_name,
                        "schema": _copy_mapping(request.response_schema),
                        "strict": True,
                    },
                }
        if request.max_output_tokens is not None:
            parameters[self._config.max_tokens_field.value] = request.max_output_tokens
        if request.temperature is not None:
            parameters["temperature"] = request.temperature
        return parameters, messages

    def _build_messages(self, request: ModelRequest) -> list[dict[str, object]]:
        continuation = request.continuation
        mapped_messages = self._map_messages(request)
        if continuation is None:
            if request.tool_outputs:
                raise ValueError("chat tool outputs require a continuation")
            messages = mapped_messages
        else:
            if not isinstance(continuation, ChatCompletionsContinuation):
                raise ValueError("continuation was not created by a compatible chat adapter")
            if not continuation._matches(
                self._owner,
                self._config.provider,
                self._config.model,
            ):
                raise ValueError("chat continuation belongs to another adapter transaction")
            continuation._validate_tool_outputs(request)
            messages = continuation._copy_messages()
            messages.extend(
                {
                    "role": "tool",
                    "tool_call_id": output.call_id,
                    "content": self._format_tool_output(output.output, output.is_error),
                }
                for output in request.tool_outputs
            )
            messages.extend(mapped_messages)
        if request.response_schema is not None:
            instruction = self._schema_instruction(request)
            if instruction not in messages:
                messages.insert(0, instruction)
        return messages

    def _map_messages(self, request: ModelRequest) -> list[dict[str, object]]:
        messages: list[dict[str, object]] = []
        for message in request.messages:
            role = message.role.value
            if message.role is MessageRole.DEVELOPER:
                role = self._config.developer_role.value
            messages.append(
                {
                    "role": role,
                    "content": message.content,
                    **({"name": message.name} if message.name is not None else {}),
                }
            )
        return messages

    @staticmethod
    def _schema_instruction(request: ModelRequest) -> dict[str, object]:
        schema = json.dumps(
            _copy_mapping(request.response_schema or {}),
            ensure_ascii=False,
            sort_keys=True,
        )
        return {
            "role": "system",
            "content": (
                "只输出一个 JSON 对象，不要输出 Markdown 或额外说明。"
                f"输出必须满足名为 {request.response_schema_name!r} 的 JSON Schema：{schema}"
            ),
        }

    def _private_continuation_fields(self, message: object) -> Mapping[str, object]:
        """提取只允许保存在不透明续轮中的供应商私有字段。

        子类可以扩展该钩子以支持供应商特有的交错推理字段。返回值只会在模型请求
        工具调用时写入 :class:`ChatCompletionsContinuation`，不会进入公开响应元数据。

        Args:
            message: 供应商返回的 assistant 消息。

        Returns:
            需要在同一工具事务下一轮原样回传的私有字段。

        Raises:
            ModelResponseParseError: 推理字段的类型不符合 Chat API 约定。
        """
        reasoning_content = self._read(message, "reasoning_content", None)
        if reasoning_content is not None and not isinstance(reasoning_content, str):
            raise ModelResponseParseError("chat reasoning content must be text or null")
        if self._config.preserve_reasoning_content and reasoning_content is not None:
            return {"reasoning_content": reasoning_content}
        return {}

    def _parse_response(
        self,
        response: object,
        history: Sequence[Mapping[str, object]],
    ) -> ModelResponse:
        choices = self._read(response, "choices", ())
        if not isinstance(choices, Sequence) or isinstance(choices, (str, bytes, bytearray)):
            raise ModelResponseParseError("chat response choices must be an array")
        if not choices:
            raise ModelResponseParseError("chat response contains no choices")
        choice = choices[0]
        message = self._read(choice, "message", None)
        if message is None:
            raise ModelResponseParseError("chat response choice has no message")
        content = self._read(message, "content", None)
        if content is not None and not isinstance(content, str):
            raise ModelResponseParseError("chat response content must be text or null")
        private_fields = self._private_continuation_fields(message)

        tool_calls, continuation_calls = self._parse_tool_calls(message)
        continuation = None
        if tool_calls:
            assistant_message: dict[str, object] = {
                "role": "assistant",
                "content": content,
                "tool_calls": continuation_calls,
            }
            assistant_message.update(_copy_mapping(private_fields))
            continuation = ChatCompletionsContinuation(
                provider=self._config.provider,
                model=self._config.model,
                owner=self._owner,
                messages=(*history, assistant_message),
                expected_tool_call_ids=tuple(call.call_id for call in tool_calls),
            )

        usage = self._parse_usage(response)
        response_id = self._read(response, "id", None)
        response_model = self._read(response, "model", self._config.model)
        finish_reason = self._read(choice, "finish_reason", None)
        return ModelResponse(
            output_text=content or "",
            tool_calls=tool_calls,
            usage=usage,
            response_id=response_id if isinstance(response_id, str) else None,
            continuation=continuation,
            metadata={
                "provider": self._config.provider,
                "model": (
                    response_model if isinstance(response_model, str) else self._config.model
                ),
                "finish_reason": finish_reason,
            },
        )

    @classmethod
    def _parse_usage(cls, response: object) -> TokenUsage:
        usage = cls._read(response, "usage", None)
        input_tokens = cls._read_int(usage, "prompt_tokens")
        output_tokens = cls._read_int(usage, "completion_tokens")
        total_tokens = cls._read_int(usage, "total_tokens") or input_tokens + output_tokens
        input_details = cls._read(usage, "prompt_tokens_details", None)
        output_details = cls._read(usage, "completion_tokens_details", None)
        cache_hit_tokens = cls._read_int(usage, "prompt_cache_hit_tokens")
        if cache_hit_tokens == 0:
            cache_hit_tokens = cls._read_int(input_details, "cached_tokens")
        raw_cache_miss = cls._read(usage, "prompt_cache_miss_tokens", None)
        cache_miss_tokens = (
            raw_cache_miss
            if isinstance(raw_cache_miss, int)
            and not isinstance(raw_cache_miss, bool)
            and raw_cache_miss >= 0
            else max(input_tokens - cache_hit_tokens, 0)
        )
        reasoning_tokens = cls._read_int(output_details, "reasoning_tokens")
        if reasoning_tokens == 0:
            reasoning_tokens = cls._read_int(usage, "reasoning_tokens")
        return TokenUsage(
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            total_tokens=total_tokens,
            cache_hit_tokens=cache_hit_tokens,
            cache_miss_tokens=cache_miss_tokens,
            reasoning_tokens=reasoning_tokens,
        )

    @classmethod
    def _parse_tool_calls(
        cls,
        message: object,
    ) -> tuple[tuple[ToolCall, ...], list[dict[str, object]]]:
        raw_calls = cls._read(message, "tool_calls", ())
        if raw_calls is None:
            return (), []
        if not isinstance(raw_calls, Sequence) or isinstance(raw_calls, (str, bytes, bytearray)):
            raise ModelResponseParseError("chat tool calls must be an array")
        calls: list[ToolCall] = []
        continuation_calls: list[dict[str, object]] = []
        for raw_call in raw_calls:
            call_id = cls._read(raw_call, "id", "")
            function = cls._read(raw_call, "function", None)
            name = cls._read(function, "name", "")
            raw_arguments = cls._read(function, "arguments", "{}")
            if not isinstance(call_id, str) or not call_id.strip():
                raise ModelResponseParseError("chat tool call id must be non-empty text")
            if not isinstance(name, str) or not name.strip():
                raise ModelResponseParseError("chat tool call name must be non-empty text")
            arguments = cls._parse_arguments(raw_arguments)
            calls.append(ToolCall(call_id=call_id, name=name, arguments=arguments))
            serialized_arguments = (
                raw_arguments
                if isinstance(raw_arguments, str)
                else json.dumps(dict(arguments), ensure_ascii=False, separators=(",", ":"))
            )
            continuation_calls.append(
                {
                    "id": call_id,
                    "type": "function",
                    "function": {"name": name, "arguments": serialized_arguments},
                }
            )
        return tuple(calls), continuation_calls

    @staticmethod
    def _read(value: object, name: str, default: object) -> object:
        if isinstance(value, Mapping):
            return value.get(name, default)
        return getattr(value, name, default)

    @classmethod
    def _read_int(cls, value: object, name: str) -> int:
        result = cls._read(value, name, 0)
        return result if isinstance(result, int) and not isinstance(result, bool) else 0

    @staticmethod
    def _parse_arguments(value: object) -> Mapping[str, object]:
        if isinstance(value, Mapping):
            return {str(key): item for key, item in value.items()}
        if not isinstance(value, str):
            raise ModelResponseParseError("chat tool arguments must be a JSON object")
        try:
            decoded = json.loads(value)
        except json.JSONDecodeError as exc:
            raise ModelResponseParseError("chat tool arguments contain invalid JSON") from exc
        if not isinstance(decoded, dict) or not all(isinstance(key, str) for key in decoded):
            raise ModelResponseParseError("chat tool arguments must decode to an object")
        return cast(dict[str, object], decoded)

    @staticmethod
    def _format_tool_output(output: str, is_error: bool) -> str:
        if not is_error:
            return output
        return json.dumps({"is_error": True, "content": output}, ensure_ascii=False)

    def _safe_invocation_error(self, error: Exception) -> ModelInvocationError:
        status_code = self._status_code(error)
        provider = self._config.provider
        if status_code in {401, 403}:
            return ModelAuthenticationError(
                f"{provider} authentication failed (HTTP {status_code})"
            )
        if status_code == 402:
            return ModelPaymentRequiredError(f"{provider} payment is required (HTTP 402)")
        if status_code == 429:
            return ModelRateLimitError(f"{provider} rate limit was exceeded (HTTP 429)")
        if status_code is not None and 500 <= status_code < 600:
            return ModelServiceError(f"{provider} service failed (HTTP {status_code})")
        return ModelInvocationError(
            f"{provider} Chat Completions call failed ({type(error).__name__})"
        )

    @classmethod
    def _status_code(cls, error: Exception) -> int | None:
        status_code = cls._read(error, "status_code", None)
        if isinstance(status_code, int) and not isinstance(status_code, bool):
            return status_code
        response = cls._read(error, "response", None)
        response_status = cls._read(response, "status_code", None)
        if isinstance(response_status, int) and not isinstance(response_status, bool):
            return response_status
        return None


__all__ = [
    "ChatCompletionsContinuation",
    "ChatDeveloperRole",
    "ChatMaxTokensField",
    "ChatStructuredOutputMode",
    "OpenAICompatibleChatClient",
    "OpenAICompatibleChatCompletionsResource",
    "OpenAICompatibleChatConfig",
    "OpenAICompatibleChatModelClient",
    "OpenAICompatibleChatResource",
    "SupportsAsyncClose",
]
