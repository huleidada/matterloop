"""将 DeepSeek 异步 Chat Completions API 适配为 MatterLoop 模型协议。"""

from __future__ import annotations

import copy
import json
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from enum import Enum
from typing import Protocol, cast

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
    ModelInvocationError,
    ModelPaymentRequiredError,
    ModelRateLimitError,
    ModelResponseParseError,
    ModelServiceError,
)

_PROVIDER = "deepseek_chat_completions"


class DeepSeekThinkingMode(str, Enum):
    """DeepSeek V4 的思考模式开关。"""

    ENABLED = "enabled"
    DISABLED = "disabled"


class DeepSeekReasoningEffort(str, Enum):
    """DeepSeek V4 官方支持的推理强度。"""

    HIGH = "high"
    MAX = "max"


class DeepSeekChatCompletionsResource(Protocol):
    """调用方注入客户端需要暴露的 Chat Completions 资源。"""

    @property
    def create(self) -> Callable[..., Awaitable[object]]:
        """返回可接受供应商关键字参数的异步请求函数。"""
        ...


class DeepSeekChatResource(Protocol):
    """调用方注入客户端需要暴露的 Chat API 资源。"""

    @property
    def completions(self) -> DeepSeekChatCompletionsResource:
        """返回异步 Chat Completions 资源。"""
        ...


class DeepSeekChatClient(Protocol):
    """DeepSeek 适配器依赖的最小异步客户端协议。"""

    @property
    def chat(self) -> DeepSeekChatResource:
        """返回异步 Chat API 资源。"""
        ...

    async def close(self) -> None:
        """关闭客户端持有的异步传输资源。"""
        ...


@dataclass(frozen=True, slots=True)
class DeepSeekModelConfig:
    """配置 DeepSeek Chat Completions 适配器。

    Args:
        model: 调用方明确选择的 DeepSeek 模型标识，不提供隐式默认模型。
        thinking_mode: 是否启用模型思考模式。
        reasoning_effort: 思考模式下的推理强度；``None`` 使用供应商默认值。
        enable_strict_tools: 是否发送仍处于 Beta 的严格工具参数，默认关闭。

    Notes:
        端点、凭据、代理、超时和 SDK 重试均属于注入客户端的构造职责。本配置不读取
        环境变量，也不保存任何连接凭据。
    """

    model: str
    thinking_mode: DeepSeekThinkingMode = DeepSeekThinkingMode.ENABLED
    reasoning_effort: DeepSeekReasoningEffort | None = None
    enable_strict_tools: bool = False

    def __post_init__(self) -> None:
        """校验模型标识和思考模式组合。"""
        if not self.model.strip():
            raise ValueError("DeepSeek model must not be empty")
        if not isinstance(self.thinking_mode, DeepSeekThinkingMode):
            raise TypeError("thinking mode must be a DeepSeekThinkingMode")
        if self.reasoning_effort is not None and not isinstance(
            self.reasoning_effort, DeepSeekReasoningEffort
        ):
            raise TypeError("reasoning effort must be a DeepSeekReasoningEffort")
        if (
            self.thinking_mode is DeepSeekThinkingMode.DISABLED
            and self.reasoning_effort is not None
        ):
            raise ValueError("reasoning effort requires DeepSeek thinking mode")


class DeepSeekChatContinuation:
    """保存 DeepSeek 工具续轮所需的私有聊天历史。

    该对象由适配器创建，上层应把它视为不透明值并原样放进下一次
    :class:`~matterloop_models.ModelRequest`。内部可能包含 ``reasoning_content``，因此
    自定义 ``repr`` 不展示聊天历史，也不提供历史内容的公共访问器。
    """

    __slots__ = ("_expected_tool_call_ids", "_messages", "_model", "_owner")

    def __init__(
        self,
        model: str,
        messages: Sequence[Mapping[str, object]],
        *,
        owner: object | None = None,
        expected_tool_call_ids: Sequence[str] = (),
    ) -> None:
        self._model = model
        self._owner = owner
        self._messages = tuple(self._clone_message(message) for message in messages)
        self._expected_tool_call_ids = tuple(expected_tool_call_ids)

    @property
    def provider(self) -> str:
        """返回 DeepSeek Chat Completions 供应商标识。"""
        return _PROVIDER

    @property
    def model(self) -> str:
        """返回创建续轮状态的非敏感模型标识。"""
        return self._model

    def __repr__(self) -> str:
        """只展示安全诊断字段，隐藏完整对话和推理内容。"""
        return f"DeepSeekChatContinuation(provider={self.provider!r}, model={self.model!r})"

    def _copy_messages(self) -> list[dict[str, object]]:
        """为下一次供应商调用复制私有历史。"""
        return [self._clone_message(message) for message in self._messages]

    def _belongs_to(self, owner: object, model: str) -> bool:
        return self._owner is owner and self._model == model

    def _validate_tool_outputs(self, request: ModelRequest) -> None:
        provided = tuple(output.call_id for output in request.tool_outputs)
        if len(set(provided)) != len(provided):
            raise ValueError("DeepSeek tool outputs must not contain duplicate call ids")
        if self._expected_tool_call_ids and set(provided) != set(self._expected_tool_call_ids):
            raise ValueError("DeepSeek tool outputs must match every pending tool call exactly")

    @staticmethod
    def _clone_message(message: Mapping[str, object]) -> dict[str, object]:
        return cast(dict[str, object], copy.deepcopy(dict(message)))


class DeepSeekChatModelClient:
    """使用调用方注入的异步客户端调用 DeepSeek Chat Completions。

    Args:
        config: 不含凭据的模型与思考模式配置。
        client: 由组合根构造且满足 :class:`DeepSeekChatClient` 的异步客户端。
        owns_client: 是否把客户端关闭责任转移给适配器；默认继续由调用方管理。

    Notes:
        适配器不导入供应商 SDK，不读取环境变量，也不会把 SDK 原始响应放入公共结果。
    """

    def __init__(
        self,
        config: DeepSeekModelConfig,
        *,
        client: DeepSeekChatClient,
        owns_client: bool = False,
    ) -> None:
        self._config = config
        self._client = client
        self._owns_client = owns_client
        self._owner = object()

    @property
    def descriptor(self) -> ModelDescriptor:
        """返回 DeepSeek 适配器的非敏感能力描述。"""
        supported = {
            ModelFeature.TEXT_GENERATION,
            ModelFeature.TOOL_CALLING,
            ModelFeature.JSON_OBJECT_OUTPUT,
            ModelFeature.OPAQUE_CONTINUATION,
            ModelFeature.REASONING,
        }
        unsupported = {
            ModelFeature.JSON_SCHEMA_OUTPUT,
            ModelFeature.RESPONSE_ID_CONTINUATION,
        }
        if self._config.thinking_mode is DeepSeekThinkingMode.DISABLED:
            supported.add(ModelFeature.TEMPERATURE)
        else:
            unsupported.add(ModelFeature.TEMPERATURE)
        return ModelDescriptor(
            provider="deepseek",
            model=self._config.model,
            capabilities=ModelCapabilities(
                supported=frozenset(supported),
                unsupported=frozenset(unsupported),
            ),
            metadata={"api": "chat_completions"},
        )

    async def generate(self, request: ModelRequest) -> ModelResponse:
        """调用 DeepSeek 并归一化文本、工具调用、续轮状态与用量。

        Args:
            request: 与供应商无关的模型请求。

        Returns:
            不包含 SDK 对象、凭据或公开推理内容的通用响应。

        Raises:
            ModelAuthenticationError: 供应商返回 401。
            ModelPaymentRequiredError: 供应商返回 402。
            ModelRateLimitError: 供应商返回 429。
            ModelServiceError: 供应商返回 5xx。
            ModelInvocationError: 供应商调用因其他原因失败。
            ModelResponseParseError: 响应结构或工具参数无法安全归一化。
        """
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
        """仅在适配器持有客户端所有权时关闭其连接池。"""
        if self._owns_client:
            await self._client.close()

    def _build_parameters(
        self, request: ModelRequest
    ) -> tuple[dict[str, object], list[dict[str, object]]]:
        messages = self._build_messages(request)
        parameters: dict[str, object] = {
            "model": self._config.model,
            "messages": messages,
            "extra_body": {"thinking": {"type": self._config.thinking_mode.value}},
        }
        if self._config.reasoning_effort is not None:
            parameters["reasoning_effort"] = self._config.reasoning_effort.value
        if request.tools:
            parameters["tools"] = [
                {
                    "type": "function",
                    "function": {
                        "name": tool.name,
                        "description": tool.description,
                        "parameters": dict(tool.parameters),
                        **({"strict": tool.strict} if self._config.enable_strict_tools else {}),
                    },
                }
                for tool in request.tools
            ]
        if request.tool_choice is not None:
            parameters["tool_choice"] = request.tool_choice.value
        if request.response_schema is not None:
            parameters["response_format"] = {"type": "json_object"}
        if request.max_output_tokens is not None:
            parameters["max_tokens"] = request.max_output_tokens
        if request.temperature is not None:
            if self._config.thinking_mode is DeepSeekThinkingMode.ENABLED:
                raise ValueError("temperature is unavailable in DeepSeek thinking mode")
            parameters["temperature"] = request.temperature
        return parameters, messages

    def _build_messages(self, request: ModelRequest) -> list[dict[str, object]]:
        continuation = request.continuation
        if continuation is None:
            if request.tool_outputs:
                raise ValueError("DeepSeek tool outputs require a chat continuation")
            messages = self._map_messages(request)
            if request.response_schema is not None:
                messages.insert(0, self._schema_instruction(request))
            return messages

        if not isinstance(continuation, DeepSeekChatContinuation):
            raise ValueError("continuation was not created by DeepSeekChatModelClient")
        if not continuation._belongs_to(self._owner, self._config.model):
            raise ValueError("DeepSeek continuation belongs to another adapter transaction")
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
        messages.extend(self._map_messages(request))
        if request.response_schema is not None:
            instruction = self._schema_instruction(request)
            if instruction not in messages:
                messages.insert(0, instruction)
        return messages

    @staticmethod
    def _map_messages(request: ModelRequest) -> list[dict[str, object]]:
        return [
            {
                "role": (
                    MessageRole.SYSTEM.value
                    if message.role is MessageRole.DEVELOPER
                    else message.role.value
                ),
                "content": message.content,
                **({"name": message.name} if message.name is not None else {}),
            }
            for message in request.messages
        ]

    @staticmethod
    def _schema_instruction(request: ModelRequest) -> dict[str, object]:
        schema = json.dumps(dict(request.response_schema or {}), ensure_ascii=False, sort_keys=True)
        return {
            "role": "system",
            "content": (
                "只输出一个 JSON 对象，不要输出 Markdown 或额外说明。"
                f"输出必须满足名为 {request.response_schema_name!r} 的 JSON Schema：{schema}"
            ),
        }

    def _parse_response(
        self,
        response: object,
        history: Sequence[Mapping[str, object]],
    ) -> ModelResponse:
        choices = self._read(response, "choices", ())
        if not isinstance(choices, Sequence) or isinstance(choices, (str, bytes, bytearray)):
            raise ModelResponseParseError("DeepSeek response choices must be an array")
        if not choices:
            raise ModelResponseParseError("DeepSeek response contains no choices")
        choice = choices[0]
        message = self._read(choice, "message", None)
        if message is None:
            raise ModelResponseParseError("DeepSeek response choice has no message")

        content = self._read(message, "content", None)
        if content is not None and not isinstance(content, str):
            raise ModelResponseParseError("DeepSeek response content must be text or null")
        reasoning_content = self._read(message, "reasoning_content", None)
        if reasoning_content is not None and not isinstance(reasoning_content, str):
            raise ModelResponseParseError(
                "DeepSeek response reasoning content must be text or null"
            )

        tool_calls, continuation_calls = self._parse_tool_calls(message)
        continuation = None
        if tool_calls:
            assistant_message: dict[str, object] = {
                "role": "assistant",
                "content": content,
                "tool_calls": continuation_calls,
            }
            if reasoning_content is not None:
                assistant_message["reasoning_content"] = reasoning_content
            continuation = DeepSeekChatContinuation(
                self._config.model,
                (*history, assistant_message),
                owner=self._owner,
                expected_tool_call_ids=tuple(call.call_id for call in tool_calls),
            )
        token_usage = self._parse_usage(response)
        response_id = self._read(response, "id", None)
        finish_reason = self._read(choice, "finish_reason", None)
        response_model = self._read(response, "model", self._config.model)
        return ModelResponse(
            output_text=content or "",
            tool_calls=tool_calls,
            usage=token_usage,
            response_id=response_id if isinstance(response_id, str) else None,
            continuation=continuation,
            metadata={
                "provider": "deepseek",
                "model": response_model if isinstance(response_model, str) else self._config.model,
                "finish_reason": finish_reason,
            },
        )

    @classmethod
    def _parse_usage(cls, response: object) -> TokenUsage:
        usage = cls._read(response, "usage", None)
        completion_details = cls._read(usage, "completion_tokens_details", None)
        input_tokens = cls._read_int(usage, "prompt_tokens")
        output_tokens = cls._read_int(usage, "completion_tokens")
        total_tokens = cls._read_int(usage, "total_tokens") or input_tokens + output_tokens
        cache_hit_tokens = cls._read_int(usage, "prompt_cache_hit_tokens")
        raw_cache_miss_tokens = cls._read(usage, "prompt_cache_miss_tokens", None)
        cache_miss_tokens = (
            raw_cache_miss_tokens
            if isinstance(raw_cache_miss_tokens, int)
            and not isinstance(raw_cache_miss_tokens, bool)
            and raw_cache_miss_tokens >= 0
            else max(input_tokens - cache_hit_tokens, 0)
        )
        return TokenUsage(
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            total_tokens=total_tokens,
            cache_hit_tokens=cache_hit_tokens,
            cache_miss_tokens=cache_miss_tokens,
            reasoning_tokens=cls._read_int(completion_details, "reasoning_tokens"),
        )

    @classmethod
    def _parse_tool_calls(
        cls, message: object
    ) -> tuple[tuple[ToolCall, ...], list[dict[str, object]]]:
        raw_calls = cls._read(message, "tool_calls", ())
        if raw_calls is None:
            return (), []
        if not isinstance(raw_calls, Sequence) or isinstance(raw_calls, (str, bytes, bytearray)):
            raise ModelResponseParseError("DeepSeek tool calls must be an array")

        calls: list[ToolCall] = []
        continuation_calls: list[dict[str, object]] = []
        for raw_call in raw_calls:
            call_id = cls._read(raw_call, "id", "")
            function = cls._read(raw_call, "function", None)
            name = cls._read(function, "name", "")
            raw_arguments = cls._read(function, "arguments", "{}")
            if not isinstance(call_id, str) or not isinstance(name, str):
                raise ModelResponseParseError("DeepSeek tool call identifiers are invalid")
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
            raise ModelResponseParseError("DeepSeek tool arguments must be a JSON object")
        try:
            decoded = json.loads(value)
        except json.JSONDecodeError as exc:
            raise ModelResponseParseError("DeepSeek tool arguments contain invalid JSON") from exc
        if not isinstance(decoded, dict) or not all(isinstance(key, str) for key in decoded):
            raise ModelResponseParseError("DeepSeek tool arguments must decode to an object")
        return cast(dict[str, object], decoded)

    @staticmethod
    def _format_tool_output(output: str, is_error: bool) -> str:
        if not is_error:
            return output
        return json.dumps({"is_error": True, "content": output}, ensure_ascii=False)

    @classmethod
    def _safe_invocation_error(cls, error: Exception) -> ModelInvocationError:
        status_code = cls._status_code(error)
        if status_code == 401:
            return ModelAuthenticationError("DeepSeek authentication failed (HTTP 401)")
        if status_code == 402:
            return ModelPaymentRequiredError("DeepSeek payment is required (HTTP 402)")
        if status_code == 429:
            return ModelRateLimitError("DeepSeek rate limit was exceeded (HTTP 429)")
        if status_code is not None and 500 <= status_code < 600:
            return ModelServiceError(f"DeepSeek service failed (HTTP {status_code})")
        return ModelInvocationError(
            f"DeepSeek Chat Completions call failed ({type(error).__name__})"
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
