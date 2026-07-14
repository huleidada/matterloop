"""DeepSeek Chat Completions 适配器的纯离线映射测试。"""

from __future__ import annotations

import asyncio
from collections import deque
from dataclasses import fields
from types import SimpleNamespace

import pytest
from matterloop_models import (
    MessageRole,
    ModelAuthenticationError,
    ModelInvocationError,
    ModelMessage,
    ModelPaymentRequiredError,
    ModelRateLimitError,
    ModelRequest,
    ModelResponseParseError,
    ModelServiceError,
    ToolChoice,
    ToolDefinition,
    ToolOutput,
)
from matterloop_models.providers import (
    DeepSeekChatModelClient,
    DeepSeekModelConfig,
    DeepSeekReasoningEffort,
    DeepSeekThinkingMode,
)


class StubCompletions:
    """记录异步调用并按队列返回响应或抛出异常。"""

    def __init__(self, *results: object) -> None:
        self._results = deque(results)
        self.parameters: list[dict[str, object]] = []

    async def create(self, **kwargs: object) -> object:
        """模拟 SDK 的异步 Chat Completions 调用。"""
        self.parameters.append(kwargs)
        result = self._results.popleft()
        if isinstance(result, Exception):
            raise result
        return result


class StubDeepSeekClient:
    """暴露与异步 OpenAI 兼容 SDK 相同的 chat 资源。"""

    def __init__(self, *results: object) -> None:
        self.completions = StubCompletions(*results)
        self.chat = SimpleNamespace(completions=self.completions)
        self.closed = False

    async def close(self) -> None:
        """记录适配器是否取得客户端关闭责任。"""
        self.closed = True


class SupplierStatusError(RuntimeError):
    """模拟可能携带敏感文本和 HTTP 状态的 SDK 异常。"""

    def __init__(self, status_code: int) -> None:
        super().__init__("authorization sk-private-value")
        self.status_code = status_code


def _tool_response(*, reasoning: str = "private chain of thought") -> object:
    return SimpleNamespace(
        id="chatcmpl_1",
        model="deepseek-v4-flash",
        choices=[
            SimpleNamespace(
                finish_reason="tool_calls",
                message=SimpleNamespace(
                    content="准备查询",
                    reasoning_content=reasoning,
                    tool_calls=[
                        SimpleNamespace(
                            id="call_1",
                            type="function",
                            function=SimpleNamespace(
                                name="lookup",
                                arguments='{"query":"matter"}',
                            ),
                        )
                    ],
                ),
            )
        ],
        usage=SimpleNamespace(
            prompt_tokens=12,
            completion_tokens=7,
            total_tokens=19,
            prompt_cache_hit_tokens=8,
            prompt_cache_miss_tokens=4,
            completion_tokens_details=SimpleNamespace(reasoning_tokens=5),
        ),
    )


def _final_response() -> object:
    return SimpleNamespace(
        id="chatcmpl_2",
        model="deepseek-v4-flash",
        choices=[
            SimpleNamespace(
                finish_reason="stop",
                message=SimpleNamespace(
                    content="查询完成",
                    reasoning_content="final private reasoning",
                    tool_calls=None,
                ),
            )
        ],
        usage=SimpleNamespace(
            prompt_tokens=20,
            completion_tokens=3,
            total_tokens=23,
            prompt_cache_hit_tokens=10,
            prompt_cache_miss_tokens=10,
            completion_tokens_details=SimpleNamespace(reasoning_tokens=1),
        ),
    )


def test_deepseek_config_contains_no_connection_or_credential_state() -> None:
    """适配器配置只能保存非敏感调用选项。"""
    config = DeepSeekModelConfig(model="deepseek-v4-flash")

    assert {item.name for item in fields(config)} == {
        "model",
        "thinking_mode",
        "reasoning_effort",
        "enable_strict_tools",
    }
    assert "key" not in repr(config).lower()


def test_deepseek_maps_schema_tools_thinking_and_usage() -> None:
    asyncio.run(_mapping_scenario())


async def _mapping_scenario() -> None:
    sdk_client = StubDeepSeekClient(_tool_response())
    client = DeepSeekChatModelClient(
        DeepSeekModelConfig(
            model="deepseek-v4-flash",
            reasoning_effort=DeepSeekReasoningEffort.MAX,
        ),
        client=sdk_client,
    )
    request = ModelRequest(
        messages=(
            ModelMessage(MessageRole.DEVELOPER, "只返回结果"),
            ModelMessage(MessageRole.USER, "查询"),
        ),
        tools=(
            ToolDefinition(
                name="lookup",
                description="查询证据",
                parameters={"type": "object", "properties": {"query": {"type": "string"}}},
            ),
        ),
        tool_choice=ToolChoice.REQUIRED,
        response_schema={"type": "object", "properties": {"answer": {"type": "string"}}},
        response_schema_name="answer_schema",
        max_output_tokens=128,
    )

    response = await client.generate(request)
    parameters = sdk_client.completions.parameters[0]

    assert parameters["model"] == "deepseek-v4-flash"
    assert parameters["extra_body"] == {"thinking": {"type": "enabled"}}
    assert parameters["reasoning_effort"] == "max"
    assert parameters["tool_choice"] == "required"
    assert parameters["response_format"] == {"type": "json_object"}
    assert parameters["max_tokens"] == 128
    messages = parameters["messages"]
    assert isinstance(messages, list)
    assert messages[0]["role"] == "system"
    assert "answer_schema" in messages[0]["content"]
    assert messages[1] == {"role": "system", "content": "只返回结果"}
    tools = parameters["tools"]
    assert isinstance(tools, list)
    assert tools[0]["function"]["name"] == "lookup"
    assert "strict" not in tools[0]["function"]

    assert response.output_text == "准备查询"
    assert response.tool_calls[0].arguments == {"query": "matter"}
    assert response.usage.input_tokens == 12
    assert response.usage.output_tokens == 7
    assert response.usage.total_tokens == 19
    assert response.usage.cache_hit_tokens == 8
    assert response.usage.cache_miss_tokens == 4
    assert response.usage.reasoning_tokens == 5
    assert response.metadata == {
        "provider": "deepseek",
        "model": "deepseek-v4-flash",
        "finish_reason": "tool_calls",
    }


def test_deepseek_continuation_returns_reasoning_only_to_supplier() -> None:
    asyncio.run(_continuation_scenario())


async def _continuation_scenario() -> None:
    private_reasoning = "never expose this private reasoning"
    sdk_client = StubDeepSeekClient(
        _tool_response(reasoning=private_reasoning),
        _final_response(),
    )
    client = DeepSeekChatModelClient(
        DeepSeekModelConfig(model="deepseek-v4-flash"),
        client=sdk_client,
    )
    first = await client.generate(
        ModelRequest(messages=(ModelMessage(MessageRole.USER, "查询证据"),))
    )

    assert first.continuation is not None
    assert private_reasoning not in repr(first)
    assert private_reasoning not in repr(first.continuation)

    second = await client.generate(
        ModelRequest(
            messages=(),
            tool_outputs=(ToolOutput("call_1", "找到证据"),),
            continuation=first.continuation,
        )
    )
    supplier_messages = sdk_client.completions.parameters[1]["messages"]

    assert supplier_messages[0] == {"role": "user", "content": "查询证据"}
    assert supplier_messages[1]["role"] == "assistant"
    assert supplier_messages[1]["reasoning_content"] == private_reasoning
    assert supplier_messages[1]["tool_calls"][0]["id"] == "call_1"
    assert supplier_messages[2] == {
        "role": "tool",
        "tool_call_id": "call_1",
        "content": "找到证据",
    }
    assert second.output_text == "查询完成"
    assert second.continuation is None
    assert "final private reasoning" not in repr(second)
    assert "reasoning_content" not in second.metadata


def test_deepseek_disabled_thinking_maps_temperature() -> None:
    async def scenario() -> None:
        sdk_client = StubDeepSeekClient(_final_response())
        client = DeepSeekChatModelClient(
            DeepSeekModelConfig(
                model="deepseek-v4-flash",
                thinking_mode=DeepSeekThinkingMode.DISABLED,
            ),
            client=sdk_client,
        )
        await client.generate(
            ModelRequest(
                messages=(ModelMessage(MessageRole.USER, "简短回答"),),
                temperature=0.2,
            )
        )

        assert sdk_client.completions.parameters[0]["temperature"] == 0.2
        assert sdk_client.completions.parameters[0]["extra_body"] == {
            "thinking": {"type": "disabled"}
        }

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("status_code", "expected_type"),
    [
        (401, ModelAuthenticationError),
        (402, ModelPaymentRequiredError),
        (429, ModelRateLimitError),
        (500, ModelServiceError),
        (503, ModelServiceError),
        (400, ModelInvocationError),
    ],
)
def test_deepseek_maps_http_errors_without_supplier_text(
    status_code: int,
    expected_type: type[ModelInvocationError],
) -> None:
    async def scenario() -> None:
        client = DeepSeekChatModelClient(
            DeepSeekModelConfig(model="deepseek-v4-flash"),
            client=StubDeepSeekClient(SupplierStatusError(status_code)),
        )
        request = ModelRequest(messages=(ModelMessage(MessageRole.USER, "hello"),))

        with pytest.raises(expected_type) as captured:
            await client.generate(request)

        assert "sk-private-value" not in str(captured.value)
        assert captured.value.__suppress_context__

    asyncio.run(scenario())


def test_deepseek_rejects_invalid_tool_arguments() -> None:
    async def scenario() -> None:
        response = _tool_response()
        response.choices[0].message.tool_calls[0].function.arguments = "{invalid"
        client = DeepSeekChatModelClient(
            DeepSeekModelConfig(model="deepseek-v4-flash"),
            client=StubDeepSeekClient(response),
        )

        with pytest.raises(ModelResponseParseError, match="invalid JSON") as captured:
            await client.generate(ModelRequest(messages=(ModelMessage(MessageRole.USER, "查询"),)))

        assert captured.value.usage is not None
        assert captured.value.usage.total_tokens == 19

    asyncio.run(scenario())


def test_deepseek_rejects_continuation_from_another_client_instance() -> None:
    """同模型的私有思考历史也不能跨注入客户端或租户回放。"""

    async def scenario() -> None:
        source_sdk = StubDeepSeekClient(_tool_response())
        target_sdk = StubDeepSeekClient(_final_response())
        source = DeepSeekChatModelClient(
            DeepSeekModelConfig(model="deepseek-v4-flash"),
            client=source_sdk,
        )
        target = DeepSeekChatModelClient(
            DeepSeekModelConfig(model="deepseek-v4-flash"),
            client=target_sdk,
        )
        first = await source.generate(
            ModelRequest(messages=(ModelMessage(MessageRole.USER, "查询"),))
        )

        with pytest.raises(ValueError, match="another adapter transaction"):
            await target.generate(
                ModelRequest(
                    messages=(),
                    tool_outputs=(ToolOutput("call_1", "结果"),),
                    continuation=first.continuation,
                )
            )

        assert target_sdk.completions.parameters == []

    asyncio.run(scenario())


def test_deepseek_can_take_ownership_of_injected_client() -> None:
    sdk_client = StubDeepSeekClient(_final_response())
    client = DeepSeekChatModelClient(
        DeepSeekModelConfig(model="deepseek-v4-flash"),
        client=sdk_client,
        owns_client=True,
    )

    asyncio.run(client.aclose())

    assert sdk_client.closed


def test_deepseek_requires_explicit_client() -> None:
    with pytest.raises(TypeError, match="client"):
        DeepSeekChatModelClient(DeepSeekModelConfig(model="deepseek-v4-flash"))
