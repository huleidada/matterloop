"""将千问 OpenAI-compatible Chat Completions 适配为 MatterLoop 模型协议。"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum
from typing import Protocol

from matterloop_models.base import ModelRequest, ToolChoice
from matterloop_models.errors import ModelCapabilityError
from matterloop_models.providers.compatible import (
    ChatCompletionsContinuation,
    ChatDeveloperRole,
    ChatMaxTokensField,
    ChatStructuredOutputMode,
    OpenAICompatibleChatClient,
    OpenAICompatibleChatConfig,
    OpenAICompatibleChatModelClient,
)


class QwenThinkingMode(str, Enum):
    """控制千问混合思考模型的本轮模式。"""

    DEFAULT = "default"
    ENABLED = "enabled"
    DISABLED = "disabled"


class QwenChatClient(OpenAICompatibleChatClient, Protocol):
    """千问适配器依赖的最小异步 Chat 客户端协议。"""


QwenChatContinuation = ChatCompletionsContinuation


@dataclass(frozen=True, slots=True)
class QwenModelConfig:
    """配置千问 Chat Completions 适配器。

    Args:
        model: 调用方明确选择的千问模型标识。
        thinking_mode: 是否显式开启或关闭思考；默认服从具体模型行为。
        thinking_budget: 可选思考 Token 预算，必须是正整数。
        preserve_thinking: 是否请求保留思考，并在私有 continuation 中原样回传。
        parallel_tool_calls: 是否允许供应商一次返回多个工具调用。

    Notes:
        凭据、地域、Workspace 域名、代理、SDK 重试和连接池由调用方构造客户端时决定。
        此配置不会读取环境变量，也不会保存 API Key 或 base URL。
    """

    model: str
    thinking_mode: QwenThinkingMode = QwenThinkingMode.DEFAULT
    thinking_budget: int | None = None
    preserve_thinking: bool = False
    parallel_tool_calls: bool = False

    def __post_init__(self) -> None:
        """校验模型标识和思考参数组合。"""
        if not self.model.strip():
            raise ValueError("Qwen model must not be empty")
        if not isinstance(self.thinking_mode, QwenThinkingMode):
            raise TypeError("thinking mode must be a QwenThinkingMode")
        if self.thinking_budget is not None and self.thinking_budget < 1:
            raise ValueError("Qwen thinking budget must be at least 1")
        if self.thinking_mode is QwenThinkingMode.DISABLED and self.thinking_budget is not None:
            raise ValueError("Qwen thinking budget requires thinking mode")
        if self.thinking_mode is QwenThinkingMode.DISABLED and self.preserve_thinking:
            raise ValueError("Qwen preserved thinking requires thinking mode")


class QwenChatModelClient(OpenAICompatibleChatModelClient):
    """使用调用方注入客户端调用千问 Chat Completions。

    Args:
        config: 不含凭据的千问模型配置。
        client: 由应用组合根创建的异步 OpenAI-compatible 客户端。
        owns_client: 是否把客户端关闭责任转移给适配器。
    """

    def __init__(
        self,
        config: QwenModelConfig,
        *,
        client: QwenChatClient,
        owns_client: bool = False,
    ) -> None:
        self._qwen_config = config
        super().__init__(
            OpenAICompatibleChatConfig(
                provider="qwen",
                model=config.model,
                developer_role=ChatDeveloperRole.SYSTEM,
                structured_output_mode=ChatStructuredOutputMode.JSON_OBJECT,
                max_tokens_field=ChatMaxTokensField.MAX_COMPLETION_TOKENS,
                enable_strict_tools=False,
                preserve_reasoning_content=config.preserve_thinking,
            ),
            client=client,
            owns_client=owns_client,
        )

    def _validate_request(self, request: ModelRequest) -> None:
        super()._validate_request(request)
        if (
            request.response_schema is not None
            and self._qwen_config.thinking_mode is QwenThinkingMode.ENABLED
        ):
            raise ModelCapabilityError(
                "Qwen JSON object output is unavailable while thinking is enabled"
            )
        if (
            request.tool_choice is ToolChoice.REQUIRED
            and self._qwen_config.thinking_mode is QwenThinkingMode.ENABLED
        ):
            raise ModelCapabilityError(
                "Qwen required tool choice is unavailable while thinking is enabled"
            )

    def _provider_parameters(self, request: ModelRequest) -> Mapping[str, object]:
        extra_body: dict[str, object] = {}
        mode = self._qwen_config.thinking_mode
        if mode is QwenThinkingMode.ENABLED:
            extra_body["enable_thinking"] = True
        elif mode is QwenThinkingMode.DISABLED:
            extra_body["enable_thinking"] = False
        elif request.response_schema is not None or request.tool_choice is ToolChoice.REQUIRED:
            # JSON Mode 和 REQUIRED 工具选择都要求确定处于非思考模式。
            extra_body["enable_thinking"] = False
        if self._qwen_config.thinking_budget is not None:
            extra_body["thinking_budget"] = self._qwen_config.thinking_budget
        if self._qwen_config.preserve_thinking:
            extra_body["preserve_thinking"] = True

        parameters: dict[str, object] = {}
        if extra_body:
            parameters["extra_body"] = extra_body
        if request.tools and self._qwen_config.parallel_tool_calls:
            parameters["parallel_tool_calls"] = True
        return parameters


__all__ = [
    "QwenChatClient",
    "QwenChatContinuation",
    "QwenChatModelClient",
    "QwenModelConfig",
    "QwenThinkingMode",
]
