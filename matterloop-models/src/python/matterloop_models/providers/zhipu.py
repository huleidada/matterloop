"""将智谱 GLM Chat Completions 适配为 MatterLoop 模型协议。"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import Enum
from typing import Protocol

from matterloop_models.base import ModelRequest, ModelResponse, ToolChoice
from matterloop_models.errors import (
    ModelAuthenticationError,
    ModelCapabilityError,
    ModelInvocationError,
    ModelPaymentRequiredError,
    ModelRateLimitError,
    ModelServiceError,
)
from matterloop_models.providers.compatible import (
    ChatCompletionsContinuation,
    ChatDeveloperRole,
    ChatMaxTokensField,
    ChatStructuredOutputMode,
    OpenAICompatibleChatClient,
    OpenAICompatibleChatConfig,
    OpenAICompatibleChatModelClient,
)

_AUTHENTICATION_CODES = frozenset({"1000", "1001", "1002", "1003", "1004"})
_RATE_LIMIT_CODES = frozenset({"1302", "1303", "1304", "1305", "1308", "1310", "1313"})
_SERVICE_CODES = frozenset({"500", "1120", "1230", "1234", "1312"})


class ZhipuThinkingMode(str, Enum):
    """控制智谱 GLM 的本轮思考模式。"""

    DEFAULT = "default"
    ENABLED = "enabled"
    DISABLED = "disabled"


class ZhipuReasoningEffort(str, Enum):
    """智谱 GLM 当前接受的推理强度值。"""

    NONE = "none"
    MINIMAL = "minimal"
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    XHIGH = "xhigh"
    MAX = "max"


class ZhipuChatClient(OpenAICompatibleChatClient, Protocol):
    """智谱适配器依赖的最小异步 Chat 客户端协议。"""


ZhipuChatContinuation = ChatCompletionsContinuation


@dataclass(frozen=True, slots=True)
class ZhipuModelConfig:
    """配置智谱 GLM Chat Completions 适配器。

    Args:
        model: 调用方明确选择的 GLM 模型标识。
        thinking_mode: 是否显式开启或关闭本轮思考。
        reasoning_effort: 可选推理强度，由具体模型决定是否支持。
        clear_thinking: 是否让供应商清除历史思考；关闭时会原样回传私有思考历史。
        do_sample: 可选采样开关；``None`` 表示采用供应商默认行为。

    Notes:
        配置不包含 API Key、端点或请求头。调用方可以注入官方 Z.ai SDK 的异步客户端，
        也可以注入以智谱 base URL 构造的 OpenAI-compatible 异步客户端。
    """

    model: str
    thinking_mode: ZhipuThinkingMode = ZhipuThinkingMode.DEFAULT
    reasoning_effort: ZhipuReasoningEffort | None = None
    clear_thinking: bool = True
    do_sample: bool | None = None

    def __post_init__(self) -> None:
        """校验模型标识和思考参数组合。"""
        if not self.model.strip():
            raise ValueError("Zhipu model must not be empty")
        if not isinstance(self.thinking_mode, ZhipuThinkingMode):
            raise TypeError("thinking mode must be a ZhipuThinkingMode")
        if self.reasoning_effort is not None and not isinstance(
            self.reasoning_effort,
            ZhipuReasoningEffort,
        ):
            raise TypeError("reasoning effort must be a ZhipuReasoningEffort")
        if self.thinking_mode is ZhipuThinkingMode.DISABLED and self.reasoning_effort is not None:
            raise ValueError("Zhipu reasoning effort requires thinking mode")
        if self.thinking_mode is not ZhipuThinkingMode.ENABLED and not self.clear_thinking:
            raise ValueError("Zhipu preserved thinking requires enabled thinking mode")


class ZhipuChatModelClient(OpenAICompatibleChatModelClient):
    """使用调用方注入客户端调用智谱 GLM Chat Completions。

    Args:
        config: 不含凭据的智谱模型配置。
        client: 由应用组合根创建的异步 OpenAI-compatible 客户端。
        owns_client: 是否把客户端关闭责任转移给适配器。
    """

    def __init__(
        self,
        config: ZhipuModelConfig,
        *,
        client: ZhipuChatClient,
        owns_client: bool = False,
    ) -> None:
        self._zhipu_config = config
        super().__init__(
            OpenAICompatibleChatConfig(
                provider="zhipu",
                model=config.model,
                developer_role=ChatDeveloperRole.SYSTEM,
                structured_output_mode=ChatStructuredOutputMode.JSON_OBJECT,
                max_tokens_field=ChatMaxTokensField.MAX_TOKENS,
                enable_strict_tools=False,
                # GLM 的交错工具思考要求原样回传 reasoning_content。
                preserve_reasoning_content=True,
            ),
            client=client,
            owns_client=owns_client,
        )

    def _validate_request(self, request: ModelRequest) -> None:
        super()._validate_request(request)
        if request.tool_choice is ToolChoice.REQUIRED:
            raise ModelCapabilityError("Zhipu GLM currently supports only automatic tool choice")
        if request.temperature is not None and request.temperature > 1:
            raise ModelCapabilityError("Zhipu GLM temperature must not exceed 1")

    def _provider_parameters(self, request: ModelRequest) -> Mapping[str, object]:
        del request
        extra_body: dict[str, object] = {}
        if self._zhipu_config.thinking_mode is ZhipuThinkingMode.ENABLED:
            extra_body["thinking"] = {
                "type": "enabled",
                "clear_thinking": self._zhipu_config.clear_thinking,
            }
        elif self._zhipu_config.thinking_mode is ZhipuThinkingMode.DISABLED:
            extra_body["thinking"] = {"type": "disabled"}
        if self._zhipu_config.do_sample is not None:
            extra_body["do_sample"] = self._zhipu_config.do_sample

        parameters: dict[str, object] = {}
        if extra_body:
            parameters["extra_body"] = extra_body
        if self._zhipu_config.reasoning_effort is not None:
            parameters["reasoning_effort"] = self._zhipu_config.reasoning_effort.value
        return parameters

    def _build_parameters(
        self,
        request: ModelRequest,
    ) -> tuple[dict[str, object], list[dict[str, object]]]:
        parameters, messages = super()._build_parameters(request)
        if request.tool_choice is ToolChoice.NONE:
            # 智谱当前只接受 auto；不发送工具等价于禁止本轮工具调用。
            parameters.pop("tools", None)
            parameters.pop("tool_choice", None)
        return parameters, messages

    def _map_messages(self, request: ModelRequest) -> list[dict[str, object]]:
        messages = super()._map_messages(request)
        for message in messages:
            message.pop("name", None)
        return messages

    def _parse_response(
        self,
        response: object,
        history: Sequence[Mapping[str, object]],
    ) -> ModelResponse:
        result = super()._parse_response(response, history)
        usage = self._read(response, "usage", None)
        details = self._read(usage, "completion_tokens_details", None)
        reasoning_value = self._read(details, "reasoning_tokens", None)
        metadata = dict(result.metadata)
        metadata["reasoning_tokens_reported"] = isinstance(reasoning_value, int) and not isinstance(
            reasoning_value,
            bool,
        )
        return ModelResponse(
            output_text=result.output_text,
            tool_calls=result.tool_calls,
            usage=result.usage,
            response_id=result.response_id,
            continuation=result.continuation,
            metadata=metadata,
        )

    def _safe_invocation_error(self, error: Exception) -> ModelInvocationError:
        business_code = self._business_code(error)
        if business_code in _AUTHENTICATION_CODES:
            return ModelAuthenticationError(
                f"Zhipu authentication failed (business code {business_code})"
            )
        if business_code == "1113":
            return ModelPaymentRequiredError("Zhipu payment is required (business code 1113)")
        if business_code in _RATE_LIMIT_CODES:
            return ModelRateLimitError(
                f"Zhipu rate limit was exceeded (business code {business_code})"
            )
        if business_code in _SERVICE_CODES:
            return ModelServiceError(f"Zhipu service failed (business code {business_code})")
        return super()._safe_invocation_error(error)

    @classmethod
    def _business_code(cls, error: Exception) -> str | None:
        candidates = (
            error,
            cls._read(error, "body", None),
            cls._read(error, "error", None),
        )
        for candidate in candidates:
            if candidate is None:
                continue
            nested_error = cls._read(candidate, "error", None)
            values = (cls._read(candidate, "code", None), cls._read(nested_error, "code", None))
            for value in values:
                if isinstance(value, (str, int)) and not isinstance(value, bool):
                    return str(value)
        return None


GLMChatModelClient = ZhipuChatModelClient
GLMModelConfig = ZhipuModelConfig
GLMThinkingMode = ZhipuThinkingMode
GLMReasoningEffort = ZhipuReasoningEffort


__all__ = [
    "GLMChatModelClient",
    "GLMModelConfig",
    "GLMReasoningEffort",
    "GLMThinkingMode",
    "ZhipuChatClient",
    "ZhipuChatContinuation",
    "ZhipuChatModelClient",
    "ZhipuModelConfig",
    "ZhipuReasoningEffort",
    "ZhipuThinkingMode",
]
