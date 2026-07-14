"""MiniMax Chat Completions 适配器的纯离线契约测试。"""

from __future__ import annotations

import asyncio
from collections import deque
from dataclasses import fields
from types import SimpleNamespace

import pytest
from matterloop_models.base import (
    MessageRole,
    ModelMessage,
    ModelRequest,
    ToolChoice,
    ToolDefinition,
    ToolOutput,
)
from matterloop_models.capabilities import ModelFeature
from matterloop_models.errors import (
    ModelAuthenticationError,
    ModelCapabilityError,
    ModelInvocationError,
    ModelPaymentRequiredError,
    ModelRateLimitError,
    ModelResponseParseError,
    ModelServiceError,
)
from matterloop_models.providers.minimax import MiniMaxChatModelClient, MiniMaxModelConfig


class StubCompletions:
    """记录异步调用并按队列返回 MiniMax 离线响应。"""

    def __init__(self, *results: object) -> None:
        self._results = deque(results)
        self.parameters: list[dict[str, object]] = []

    async def create(self, **kwargs: object) -> object:
        """模拟异步 Chat Completions 调用。"""
        self.parameters.append(kwargs)
        result = self._results.popleft()
        if isinstance(result, Exception):
            raise result
        return result


class StubMiniMaxClient:
    """提供调用方创建并注入的最小 MiniMax 客户端。"""

    def __init__(self, *results: object) -> None:
        self.completions = StubCompletions(*results)
        self.chat = SimpleNamespace(completions=self.completions)
        self.closed = False

    async def close(self) -> None:
        """记录客户端资源是否由适配器关闭。"""
        self.closed = True


class SupplierRateLimitError(RuntimeError):
    """模拟正文中意外携带凭据的供应商限流异常。"""

    def __init__(self) -> None:
        super().__init__("Authorization: Bearer sk-sensitive-minimax")
        self.status_code = 429


def _lookup_tool() -> ToolDefinition:
    """构造一个无副作用的证据查询工具定义。"""
    return ToolDefinition(
        name="lookup_evidence",
        description="查询内存证据",
        parameters={
            "type": "object",
            "properties": {"query": {"type": "string"}},
            "required": ["query"],
        },
    )


def _text_response() -> object:
    """构造包含缓存和推理 Token 的普通文本响应。"""
    return SimpleNamespace(
        id="chatcmpl-minimax-final",
        model="MiniMax-M2.7",
        choices=[
            SimpleNamespace(
                finish_reason="stop",
                message=SimpleNamespace(
                    content='{"answer":"ok"}',
                    reasoning_content=None,
                    reasoning_details=[{"type": "reasoning.text", "text": "final-private"}],
                    tool_calls=None,
                ),
            )
        ],
        usage=SimpleNamespace(
            prompt_tokens=40,
            completion_tokens=12,
            total_tokens=52,
            prompt_tokens_details=SimpleNamespace(cached_tokens=24),
            completion_tokens_details=SimpleNamespace(reasoning_tokens=7),
        ),
    )


def _tool_response(reasoning_details: object) -> object:
    """构造包含工具调用和私有交错推理状态的响应。"""
    return SimpleNamespace(
        id="chatcmpl-minimax-tool",
        model="MiniMax-M2.7",
        choices=[
            SimpleNamespace(
                finish_reason="tool_calls",
                message=SimpleNamespace(
                    content="准备查询证据",
                    reasoning_content="private-reasoning-content",
                    reasoning_details=reasoning_details,
                    tool_calls=[
                        SimpleNamespace(
                            id="call-evidence",
                            type="function",
                            function=SimpleNamespace(
                                name="lookup_evidence",
                                arguments='{"query":"MatterLoop"}',
                            ),
                        ),
                        SimpleNamespace(
                            id="call-summary",
                            type="function",
                            function=SimpleNamespace(
                                name="lookup_evidence",
                                arguments='{"query":"MatterLoop summary"}',
                            ),
                        ),
                    ],
                ),
            )
        ],
        usage=SimpleNamespace(
            prompt_tokens=30,
            completion_tokens=10,
            total_tokens=40,
            prompt_tokens_details=SimpleNamespace(cached_tokens=18),
            completion_tokens_details=SimpleNamespace(reasoning_tokens=6),
        ),
    )


def test_minimax_maps_parameters_schema_prompt_and_descriptor() -> None:
    """适配器应映射 MiniMax 参数并以提示词约束结构化输出。"""

    async def scenario() -> None:
        config = MiniMaxModelConfig(model=" MiniMax-M2.7 ")
        sdk_client = StubMiniMaxClient(_text_response())
        client = MiniMaxChatModelClient(config, client=sdk_client)
        response = await client.generate(
            ModelRequest(
                messages=(
                    ModelMessage(MessageRole.DEVELOPER, "严格遵守输出格式"),
                    ModelMessage(MessageRole.USER, "给出结论"),
                ),
                tools=(_lookup_tool(),),
                tool_choice=ToolChoice.AUTO,
                response_schema={
                    "type": "object",
                    "properties": {"answer": {"type": "string"}},
                    "required": ["answer"],
                },
                response_schema_name="minimax_answer",
                max_output_tokens=1024,
                temperature=0.8,
            )
        )

        assert {item.name for item in fields(config)} == {"model"}
        assert config.model == "MiniMax-M2.7"
        parameters = sdk_client.completions.parameters[0]
        assert parameters["model"] == "MiniMax-M2.7"
        assert parameters["extra_body"] == {"reasoning_split": True}
        assert parameters["max_completion_tokens"] == 1024
        assert "max_tokens" not in parameters
        assert parameters["temperature"] == 0.8
        assert "tool_choice" not in parameters
        assert "response_format" not in parameters
        messages = parameters["messages"]
        assert isinstance(messages, list)
        assert messages[0]["role"] == "system"
        assert "minimax_answer" in messages[0]["content"]
        assert messages[1] == {"role": "system", "content": "严格遵守输出格式"}
        tools = parameters["tools"]
        assert isinstance(tools, list)
        assert "strict" not in tools[0]["function"]

        assert client.descriptor.provider == "minimax"
        assert client.descriptor.model == "MiniMax-M2.7"
        assert client.descriptor.capabilities.supports(ModelFeature.REASONING)
        assert response.output_text == '{"answer":"ok"}'
        assert "final-private" not in response.output_text
        assert response.usage.input_tokens == 40
        assert response.usage.output_tokens == 12
        assert response.usage.total_tokens == 52
        assert response.usage.cache_hit_tokens == 24
        assert response.usage.cache_miss_tokens == 16
        assert response.usage.reasoning_tokens == 7

    asyncio.run(scenario())


def test_minimax_tool_continuation_preserves_private_reasoning_details() -> None:
    """工具续轮应深复制 reasoning_details，并避免通过 repr 或公开文本泄露。"""

    async def scenario() -> None:
        private_text = "private-interleaved-reasoning"
        reasoning_details = [
            {
                "type": "reasoning.text",
                "text": private_text,
                "signature": "private-signature",
            }
        ]
        sdk_client = StubMiniMaxClient(
            _tool_response(reasoning_details),
            _text_response(),
        )
        client = MiniMaxChatModelClient(
            MiniMaxModelConfig(model="MiniMax-M2.7"),
            client=sdk_client,
        )

        first = await client.generate(
            ModelRequest(
                messages=(ModelMessage(MessageRole.USER, "查询证据"),),
                tools=(_lookup_tool(),),
            )
        )
        assert first.continuation is not None
        assert first.output_text == "准备查询证据"
        assert private_text not in first.output_text
        assert private_text not in repr(first)
        assert private_text not in repr(first.continuation)

        # continuation 必须与供应商响应对象解耦，避免调用方修改 SDK 对象污染续轮。
        reasoning_details[0]["text"] = "mutated-after-response"
        await client.generate(
            ModelRequest(
                messages=(),
                tools=(_lookup_tool(),),
                tool_outputs=(
                    ToolOutput("call-evidence", "证据已找到"),
                    ToolOutput("call-summary", "摘要已找到"),
                ),
                continuation=first.continuation,
            )
        )
        supplier_messages = sdk_client.completions.parameters[1]["messages"]
        assert isinstance(supplier_messages, list)
        assistant_message = supplier_messages[1]
        assert assistant_message["role"] == "assistant"
        assert assistant_message["reasoning_details"] == [
            {
                "type": "reasoning.text",
                "text": private_text,
                "signature": "private-signature",
            }
        ]
        assert assistant_message["reasoning_content"] == "private-reasoning-content"
        assert supplier_messages[2] == {
            "role": "tool",
            "tool_call_id": "call-evidence",
            "content": "证据已找到",
        }
        assert supplier_messages[3] == {
            "role": "tool",
            "tool_call_id": "call-summary",
            "content": "摘要已找到",
        }

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "reasoning_details",
    ["private reasoning", [{"text": "valid"}, "invalid-entry"]],
)
def test_minimax_rejects_invalid_reasoning_details(reasoning_details: object) -> None:
    """私有推理明细必须是由对象组成的非字符串序列。"""

    async def scenario() -> None:
        client = MiniMaxChatModelClient(
            MiniMaxModelConfig(model="MiniMax-M2.7"),
            client=StubMiniMaxClient(_tool_response(reasoning_details)),
        )
        with pytest.raises(ModelResponseParseError, match="reasoning detail"):
            await client.generate(
                ModelRequest(messages=(ModelMessage(MessageRole.USER, "调用工具"),))
            )

    asyncio.run(scenario())


def test_minimax_none_omits_tools_and_required_is_rejected() -> None:
    """NONE 通过不发送工具实现，REQUIRED 在供应商调用前被拒绝。"""

    async def scenario() -> None:
        sdk_client = StubMiniMaxClient(_text_response())
        client = MiniMaxChatModelClient(
            MiniMaxModelConfig(model="MiniMax-M2.7"),
            client=sdk_client,
        )
        await client.generate(
            ModelRequest(
                messages=(ModelMessage(MessageRole.USER, "不要调用工具"),),
                tools=(_lookup_tool(),),
                tool_choice=ToolChoice.NONE,
            )
        )
        assert "tools" not in sdk_client.completions.parameters[0]
        assert "tool_choice" not in sdk_client.completions.parameters[0]

        with pytest.raises(ModelCapabilityError, match="required tool choice"):
            await client.generate(
                ModelRequest(
                    messages=(ModelMessage(MessageRole.USER, "必须调用工具"),),
                    tools=(_lookup_tool(),),
                    tool_choice=ToolChoice.REQUIRED,
                )
            )
        assert len(sdk_client.completions.parameters) == 1

    asyncio.run(scenario())


@pytest.mark.parametrize("temperature", [2.000001, float("nan"), float("inf")])
def test_minimax_rejects_temperature_outside_supported_range(temperature: float) -> None:
    """MiniMax 温度必须是 ``[0, 2]`` 内的有限值。"""

    async def scenario() -> None:
        sdk_client = StubMiniMaxClient()
        client = MiniMaxChatModelClient(
            MiniMaxModelConfig(model="MiniMax-M2.7"),
            client=sdk_client,
        )
        with pytest.raises(ModelCapabilityError, match="temperature"):
            await client.generate(
                ModelRequest(
                    messages=(ModelMessage(MessageRole.USER, "回答"),),
                    temperature=temperature,
                )
            )
        assert sdk_client.completions.parameters == []

    asyncio.run(scenario())


@pytest.mark.parametrize("temperature", [0.0, 2.0])
def test_minimax_accepts_temperature_boundaries(temperature: float) -> None:
    """MiniMax 官方温度区间的两个端点都应透传。"""

    async def scenario() -> None:
        sdk_client = StubMiniMaxClient(_text_response())
        client = MiniMaxChatModelClient(
            MiniMaxModelConfig(model="MiniMax-M2.7"),
            client=sdk_client,
        )
        await client.generate(
            ModelRequest(
                messages=(ModelMessage(MessageRole.USER, "回答"),),
                temperature=temperature,
            )
        )
        assert sdk_client.completions.parameters[0]["temperature"] == temperature

    asyncio.run(scenario())


def test_minimax_sanitizes_supplier_exception_text() -> None:
    """通用错误映射不得传播 SDK 异常中的凭据或请求正文。"""

    async def scenario() -> None:
        client = MiniMaxChatModelClient(
            MiniMaxModelConfig(model="MiniMax-M2.7"),
            client=StubMiniMaxClient(SupplierRateLimitError()),
        )
        with pytest.raises(ModelRateLimitError) as captured:
            await client.generate(ModelRequest(messages=(ModelMessage(MessageRole.USER, "调用"),)))

        assert "sk-sensitive-minimax" not in str(captured.value)
        assert "authorization" not in str(captured.value).lower()
        assert captured.value.__suppress_context__

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("code", "error_type"),
    [
        (1004, ModelAuthenticationError),
        (2049, ModelAuthenticationError),
        (1008, ModelPaymentRequiredError),
        (1002, ModelRateLimitError),
        (2056, ModelRateLimitError),
        (1000, ModelServiceError),
        (1041, ModelServiceError),
        (2013, ModelInvocationError),
    ],
)
def test_minimax_maps_business_errors_without_leaking_status_message(
    code: int,
    error_type: type[ModelInvocationError],
) -> None:
    """HTTP 200 内的 MiniMax 非零业务状态也必须映射为安全类型异常。"""

    async def scenario() -> None:
        response = SimpleNamespace(
            base_resp=SimpleNamespace(
                status_code=code,
                status_msg="Authorization: Bearer sk-private-minimax",
            )
        )
        client = MiniMaxChatModelClient(
            MiniMaxModelConfig(model="MiniMax-M2.7"),
            client=StubMiniMaxClient(response),
        )
        with pytest.raises(error_type) as captured:
            await client.generate(ModelRequest(messages=(ModelMessage(MessageRole.USER, "调用"),)))

        assert str(code) in str(captured.value)
        assert "sk-private-minimax" not in str(captured.value)
        assert "authorization" not in str(captured.value).lower()

    asyncio.run(scenario())


def test_minimax_accepts_zero_business_status_and_rejects_invalid_status_type() -> None:
    """零业务状态继续解析，无法解释的状态类型应作为响应格式错误。"""

    async def scenario() -> None:
        successful_response = _text_response()
        successful_response.base_resp = SimpleNamespace(status_code=0, status_msg="")
        successful = MiniMaxChatModelClient(
            MiniMaxModelConfig(model="MiniMax-M2.7"),
            client=StubMiniMaxClient(successful_response),
        )
        result = await successful.generate(
            ModelRequest(messages=(ModelMessage(MessageRole.USER, "调用"),))
        )
        assert result.output_text == '{"answer":"ok"}'

        for invalid_status in (False, "", [1004]):
            invalid = MiniMaxChatModelClient(
                MiniMaxModelConfig(model="MiniMax-M2.7"),
                client=StubMiniMaxClient(
                    SimpleNamespace(base_resp=SimpleNamespace(status_code=invalid_status))
                ),
            )
            with pytest.raises(ModelResponseParseError, match="business status code"):
                await invalid.generate(
                    ModelRequest(messages=(ModelMessage(MessageRole.USER, "调用"),))
                )

    asyncio.run(scenario())


def test_minimax_honors_injected_client_ownership() -> None:
    """MiniMax 适配器只关闭显式交由其管理的注入客户端。"""

    async def scenario() -> None:
        borrowed_sdk = StubMiniMaxClient()
        borrowed = MiniMaxChatModelClient(
            MiniMaxModelConfig(model="MiniMax-M2.7"),
            client=borrowed_sdk,
        )
        await borrowed.aclose()
        assert not borrowed_sdk.closed

        owned_sdk = StubMiniMaxClient()
        owned = MiniMaxChatModelClient(
            MiniMaxModelConfig(model="MiniMax-M2.7"),
            client=owned_sdk,
            owns_client=True,
        )
        await owned.aclose()
        assert owned_sdk.closed

    asyncio.run(scenario())


def test_minimax_config_rejects_empty_or_multiline_model() -> None:
    """模型标识必须是明确的非空单行文本。"""
    with pytest.raises(ValueError, match="model"):
        MiniMaxModelConfig(model="  ")
    with pytest.raises(ValueError, match="single-line"):
        MiniMaxModelConfig(model="MiniMax-M2.7\nother")
