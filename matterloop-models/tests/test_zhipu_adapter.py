"""智谱 GLM Chat Completions 适配器的纯离线映射测试。"""

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
from matterloop_models.errors import (
    ModelCapabilityError,
    ModelPaymentRequiredError,
    ModelRateLimitError,
)
from matterloop_models.providers import (
    ZhipuChatModelClient,
    ZhipuModelConfig,
    ZhipuReasoningEffort,
    ZhipuThinkingMode,
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


class StubZhipuClient:
    """暴露 OpenAI-compatible chat 资源。"""

    def __init__(self, *results: object) -> None:
        self.completions = StubCompletions(*results)
        self.chat = SimpleNamespace(completions=self.completions)


class SupplierError(RuntimeError):
    """模拟同时携带敏感文本、HTTP 状态和智谱业务码的错误。"""

    def __init__(self, status_code: int, business_code: str) -> None:
        super().__init__("authorization bearer-private-value")
        self.status_code = status_code
        self.body = {"error": {"code": business_code, "message": "private supplier body"}}


def _tool_response() -> object:
    return SimpleNamespace(
        id="glm_chat_1",
        model="configured-glm",
        choices=[
            SimpleNamespace(
                finish_reason="tool_calls",
                message=SimpleNamespace(
                    content="准备查询",
                    reasoning_content="private glm reasoning",
                    tool_calls=[
                        SimpleNamespace(
                            id="call_weather",
                            type="function",
                            function=SimpleNamespace(
                                name="weather",
                                arguments='{"city":"北京"}',
                            ),
                        )
                    ],
                ),
            )
        ],
        usage=SimpleNamespace(
            prompt_tokens=20,
            completion_tokens=8,
            total_tokens=28,
            prompt_tokens_details=SimpleNamespace(cached_tokens=12),
        ),
    )


def _final_response() -> object:
    return SimpleNamespace(
        id="glm_chat_2",
        model="configured-glm",
        choices=[
            SimpleNamespace(
                finish_reason="stop",
                message=SimpleNamespace(
                    content="北京天气证据已返回",
                    reasoning_content="private final reasoning",
                    tool_calls=None,
                ),
            )
        ],
        usage=SimpleNamespace(
            prompt_tokens=32,
            completion_tokens=6,
            total_tokens=38,
            prompt_tokens_details=SimpleNamespace(cached_tokens=16),
        ),
    )


def test_zhipu_config_contains_no_connection_or_credential_state() -> None:
    """智谱配置只保存模型推理选项。"""
    config = ZhipuModelConfig(model="configured-glm")

    assert {item.name for item in fields(config)} == {
        "model",
        "thinking_mode",
        "reasoning_effort",
        "clear_thinking",
        "do_sample",
    }
    assert "key" not in repr(config).lower()
    assert "url" not in repr(config).lower()


def test_zhipu_maps_thinking_schema_tools_and_usage() -> None:
    """智谱专用选项和通用请求应映射到各自字段。"""
    asyncio.run(_mapping_scenario())


async def _mapping_scenario() -> None:
    sdk_client = StubZhipuClient(_tool_response())
    client = ZhipuChatModelClient(
        ZhipuModelConfig(
            model="configured-glm",
            thinking_mode=ZhipuThinkingMode.ENABLED,
            reasoning_effort=ZhipuReasoningEffort.MAX,
            clear_thinking=False,
            do_sample=False,
        ),
        client=sdk_client,
    )
    response = await client.generate(
        ModelRequest(
            messages=(
                ModelMessage(MessageRole.DEVELOPER, "负责验证", name="private-name"),
                ModelMessage(MessageRole.USER, "查询北京天气"),
            ),
            tools=(
                ToolDefinition(
                    name="weather",
                    description="查询天气",
                    parameters={
                        "type": "object",
                        "properties": {"city": {"type": "string"}},
                    },
                ),
            ),
            tool_choice=ToolChoice.AUTO,
            response_schema={"type": "object", "properties": {"answer": {"type": "string"}}},
            response_schema_name="answer_schema",
            max_output_tokens=2048,
            temperature=0.4,
        )
    )
    parameters = sdk_client.completions.parameters[0]

    assert parameters["model"] == "configured-glm"
    assert parameters["extra_body"] == {
        "thinking": {"type": "enabled", "clear_thinking": False},
        "do_sample": False,
    }
    assert parameters["reasoning_effort"] == "max"
    assert parameters["response_format"] == {"type": "json_object"}
    assert parameters["max_tokens"] == 2048
    assert parameters["temperature"] == 0.4
    messages = parameters["messages"]
    assert isinstance(messages, list)
    assert messages[0]["role"] == "system"
    assert "JSON Schema" in messages[0]["content"]
    assert messages[1] == {"role": "system", "content": "负责验证"}
    tools = parameters["tools"]
    assert isinstance(tools, list)
    assert "strict" not in tools[0]["function"]

    assert response.tool_calls[0].arguments == {"city": "北京"}
    assert response.usage.input_tokens == 20
    assert response.usage.cache_hit_tokens == 12
    assert response.usage.cache_miss_tokens == 8
    assert response.usage.reasoning_tokens == 0
    assert response.metadata["reasoning_tokens_reported"] is False
    assert "private glm reasoning" not in repr(response)
    assert response.continuation is not None
    assert "private glm reasoning" not in repr(response.continuation)


def test_zhipu_tool_continuation_preserves_private_reasoning_in_supplier_history() -> None:
    """工具续轮只向同一注入客户端回传私有思考内容。"""
    asyncio.run(_continuation_scenario())


async def _continuation_scenario() -> None:
    sdk_client = StubZhipuClient(_tool_response(), _final_response())
    client = ZhipuChatModelClient(
        ZhipuModelConfig(
            model="configured-glm",
            thinking_mode=ZhipuThinkingMode.ENABLED,
            clear_thinking=False,
        ),
        client=sdk_client,
    )
    first = await client.generate(
        ModelRequest(messages=(ModelMessage(MessageRole.USER, "查询天气"),))
    )
    second = await client.generate(
        ModelRequest(
            messages=(),
            tools=(
                ToolDefinition(
                    name="weather",
                    description="查询天气",
                    parameters={"type": "object", "properties": {}},
                ),
            ),
            tool_outputs=(ToolOutput("call_weather", "晴天"),),
            continuation=first.continuation,
        )
    )
    supplier_messages = sdk_client.completions.parameters[1]["messages"]

    assert supplier_messages[0] == {"role": "user", "content": "查询天气"}
    assert supplier_messages[1]["role"] == "assistant"
    assert supplier_messages[1]["reasoning_content"] == "private glm reasoning"
    assert supplier_messages[2] == {
        "role": "tool",
        "tool_call_id": "call_weather",
        "content": "晴天",
    }
    assert second.output_text == "北京天气证据已返回"
    assert "private final reasoning" not in repr(second)


def test_zhipu_tool_choice_none_omits_tools_and_required_is_rejected() -> None:
    """NONE 通过不发送工具实现，REQUIRED 在调用供应商前快速失败。"""

    async def scenario() -> None:
        sdk_client = StubZhipuClient(_final_response())
        client = ZhipuChatModelClient(
            ZhipuModelConfig(model="configured-glm"),
            client=sdk_client,
        )
        tool = ToolDefinition(
            name="weather",
            description="查询天气",
            parameters={"type": "object", "properties": {}},
        )
        await client.generate(
            ModelRequest(
                messages=(ModelMessage(MessageRole.USER, "不要调用工具"),),
                tools=(tool,),
                tool_choice=ToolChoice.NONE,
            )
        )
        assert "tools" not in sdk_client.completions.parameters[0]
        assert "tool_choice" not in sdk_client.completions.parameters[0]

        with pytest.raises(ModelCapabilityError, match="automatic tool choice"):
            await client.generate(
                ModelRequest(
                    messages=(ModelMessage(MessageRole.USER, "必须调用工具"),),
                    tools=(tool,),
                    tool_choice=ToolChoice.REQUIRED,
                )
            )
        assert len(sdk_client.completions.parameters) == 1

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("business_code", "expected_error"),
    [("1113", ModelPaymentRequiredError), ("1302", ModelRateLimitError)],
)
def test_zhipu_business_errors_are_typed_and_redacted(
    business_code: str,
    expected_error: type[Exception],
) -> None:
    """HTTP 429 下仍按安全业务码区分欠费与限流。"""

    async def scenario() -> None:
        client = ZhipuChatModelClient(
            ZhipuModelConfig(model="configured-glm"),
            client=StubZhipuClient(SupplierError(429, business_code)),
        )
        with pytest.raises(expected_error) as captured:
            await client.generate(ModelRequest(messages=(ModelMessage(MessageRole.USER, "hello"),)))
        assert "private" not in str(captured.value)
        assert captured.value.__suppress_context__

    asyncio.run(scenario())
