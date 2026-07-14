"""将 MiniMax OpenAI-compatible Chat Completions 适配为模型协议。"""

from __future__ import annotations

import copy
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Protocol

from matterloop_models.base import ModelRequest, ModelResponse, ToolChoice
from matterloop_models.errors import (
    ModelAuthenticationError,
    ModelCapabilityError,
    ModelInvocationError,
    ModelPaymentRequiredError,
    ModelRateLimitError,
    ModelResponseParseError,
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


class MiniMaxChatClient(OpenAICompatibleChatClient, Protocol):
    """MiniMax 适配器依赖的最小异步 Chat 客户端协议。"""


MiniMaxChatContinuation = ChatCompletionsContinuation


@dataclass(frozen=True, slots=True)
class MiniMaxModelConfig:
    """配置 MiniMax Chat Completions 适配器。

    Args:
        model: 调用方明确选择的 MiniMax 模型标识。

    Notes:
        配置不包含 API Key、端点、请求头或其他连接状态。调用方负责构造并注入
        OpenAI-compatible 异步客户端。
    """

    model: str

    def __post_init__(self) -> None:
        """校验并规范模型标识。"""
        model = self.model.strip()
        if not model or "\n" in model or "\r" in model:
            raise ValueError("MiniMax model must be non-empty single-line text")
        object.__setattr__(self, "model", model)


class MiniMaxChatModelClient(OpenAICompatibleChatModelClient):
    """使用调用方注入客户端调用 MiniMax Chat Completions。

    Args:
        config: 只包含模型标识的 MiniMax 配置。
        client: 由应用组合根创建的异步 OpenAI-compatible 客户端。
        owns_client: 是否把客户端关闭责任转移给适配器。
    """

    def __init__(
        self,
        config: MiniMaxModelConfig,
        *,
        client: MiniMaxChatClient,
        owns_client: bool = False,
    ) -> None:
        super().__init__(
            OpenAICompatibleChatConfig(
                provider="minimax",
                model=config.model,
                developer_role=ChatDeveloperRole.SYSTEM,
                structured_output_mode=ChatStructuredOutputMode.PROMPT_ONLY,
                max_tokens_field=ChatMaxTokensField.MAX_COMPLETION_TOKENS,
                enable_strict_tools=False,
                # reasoning_details 仅保存在不透明工具续轮中，不进入公开响应文本。
                preserve_reasoning_content=True,
            ),
            client=client,
            owns_client=owns_client,
        )

    def _validate_request(self, request: ModelRequest) -> None:
        super()._validate_request(request)
        if request.tool_choice is ToolChoice.REQUIRED:
            raise ModelCapabilityError("MiniMax does not support required tool choice")
        if request.temperature is not None and (
            not math.isfinite(request.temperature) or not 0 <= request.temperature <= 2
        ):
            raise ModelCapabilityError("MiniMax temperature must be between 0 and 2")

    def _provider_parameters(self, request: ModelRequest) -> Mapping[str, object]:
        del request
        return {"extra_body": {"reasoning_split": True}}

    def _build_parameters(
        self,
        request: ModelRequest,
    ) -> tuple[dict[str, object], list[dict[str, object]]]:
        parameters, messages = super()._build_parameters(request)
        # 当前兼容端点没有声明 tool_choice 字段；AUTO 使用供应商默认行为。
        parameters.pop("tool_choice", None)
        if request.tool_choice is ToolChoice.NONE:
            # 不发送工具即可稳定表达本轮禁止工具调用。
            parameters.pop("tools", None)
        return parameters, messages

    def _parse_response(
        self,
        response: object,
        history: Sequence[Mapping[str, object]],
    ) -> ModelResponse:
        """检查 MiniMax 业务状态后归一化 Chat 响应。

        Args:
            response: 调用方注入客户端返回的响应对象。
            history: 本轮发送给供应商的消息历史。

        Returns:
            不包含供应商原始对象和推理明细的通用响应。

        Raises:
            ModelInvocationError: ``base_resp`` 报告非零业务状态。
            ModelResponseParseError: ``base_resp.status_code`` 类型无效。
        """
        self._raise_for_business_error(response)
        return super()._parse_response(response, history)

    def _private_continuation_fields(self, message: object) -> Mapping[str, object]:
        private_fields = dict(super()._private_continuation_fields(message))
        reasoning_details = self._read(message, "reasoning_details", None)
        if reasoning_details is None:
            return private_fields
        if not isinstance(reasoning_details, Sequence) or isinstance(
            reasoning_details,
            (str, bytes, bytearray),
        ):
            raise ModelResponseParseError("MiniMax reasoning details must be an array")

        copied_details: list[dict[object, object]] = []
        for detail in reasoning_details:
            if not isinstance(detail, Mapping):
                raise ModelResponseParseError("MiniMax reasoning detail entries must be objects")
            copied_details.append(copy.deepcopy(dict(detail)))
        private_fields["reasoning_details"] = copied_details
        return private_fields

    def _raise_for_business_error(self, response: object) -> None:
        base_response = self._read(response, "base_resp", None)
        if base_response is None:
            return
        raw_code = self._read(base_response, "status_code", 0)
        if raw_code is None:
            return
        if not isinstance(raw_code, (str, int)) or isinstance(raw_code, bool):
            raise ModelResponseParseError("MiniMax business status code is invalid")
        code = str(raw_code).strip()
        if not code:
            raise ModelResponseParseError("MiniMax business status code is invalid")
        if code == "0":
            return
        if code in {"1004", "2049"}:
            raise ModelAuthenticationError(f"MiniMax authentication failed (business code {code})")
        if code == "1008":
            raise ModelPaymentRequiredError(f"MiniMax payment is required (business code {code})")
        if code in {"1002", "2056"}:
            raise ModelRateLimitError(f"MiniMax rate limit was exceeded (business code {code})")
        if code in {"1000", "1001", "1024", "1033", "1041"}:
            raise ModelServiceError(f"MiniMax service failed (business code {code})")
        raise ModelInvocationError(f"MiniMax request failed (business code {code})")


__all__ = [
    "MiniMaxChatClient",
    "MiniMaxChatContinuation",
    "MiniMaxChatModelClient",
    "MiniMaxModelConfig",
]
