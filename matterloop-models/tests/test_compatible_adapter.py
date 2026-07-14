"""OpenAI-compatible Chat Completions 通用适配器的离线契约测试。"""

from __future__ import annotations

import asyncio
from collections import deque
from types import SimpleNamespace

import pytest
from matterloop_models.base import (
    MessageRole,
    ModelMessage,
    ModelRequest,
    ToolDefinition,
    ToolOutput,
)
from matterloop_models.errors import (
    ModelAuthenticationError,
    ModelInvocationError,
    ModelPaymentRequiredError,
    ModelRateLimitError,
    ModelResponseParseError,
    ModelServiceError,
)
from matterloop_models.providers import (
    ChatDeveloperRole,
    ChatMaxTokensField,
    ChatStructuredOutputMode,
    OpenAICompatibleChatConfig,
    OpenAICompatibleChatModelClient,
)


class StubCompletions:
    """记录 Chat Completions 参数并按队列返回离线结果。"""

    def __init__(self, *results: object) -> None:
        self._results = deque(results)
        self.parameters: list[dict[str, object]] = []

    async def create(self, **kwargs: object) -> object:
        """模拟供应商 SDK 的异步创建调用。"""
        self.parameters.append(kwargs)
        result = self._results.popleft()
        if isinstance(result, Exception):
            raise result
        return result


class StubCompatibleClient:
    """提供最小 Chat 资源，并记录客户端关闭责任。"""

    def __init__(self, *results: object) -> None:
        self.completions = StubCompletions(*results)
        self.chat = SimpleNamespace(completions=self.completions)
        self.closed = False

    async def close(self) -> None:
        """记录适配器是否关闭了注入客户端。"""
        self.closed = True


class StubNonClosableClient:
    """模拟不支持异步关闭、但可由调用方管理的客户端。"""

    def __init__(self) -> None:
        self.completions = StubCompletions()
        self.chat = SimpleNamespace(completions=self.completions)


class SupplierStatusError(RuntimeError):
    """模拟错误文本包含请求凭据的供应商异常。"""

    def __init__(self, status_code: int) -> None:
        super().__init__("authorization: Bearer sk-never-log-this")
        self.status_code = status_code


def _text_response(*, usage: object | None = None) -> object:
    """构造不依赖真实 SDK 的普通文本响应。"""
    return SimpleNamespace(
        id="chatcmpl-final",
        model="compatible-model",
        choices=[
            SimpleNamespace(
                finish_reason="stop",
                message=SimpleNamespace(
                    content='{"answer":"ok"}',
                    reasoning_content=None,
                    tool_calls=None,
                ),
            )
        ],
        usage=usage
        or SimpleNamespace(
            prompt_tokens=8,
            completion_tokens=4,
            total_tokens=12,
        ),
    )


def _multi_tool_response(*, reasoning: str = "private-reasoning") -> object:
    """构造包含两个工具调用和私有推理内容的响应。"""
    return SimpleNamespace(
        id="chatcmpl-tools",
        model="compatible-model",
        choices=[
            SimpleNamespace(
                finish_reason="tool_calls",
                message=SimpleNamespace(
                    content="正在查询",
                    reasoning_content=reasoning,
                    tool_calls=[
                        SimpleNamespace(
                            id="call-a",
                            type="function",
                            function=SimpleNamespace(
                                name="lookup_a",
                                arguments='{"query":"A"}',
                            ),
                        ),
                        SimpleNamespace(
                            id="call-b",
                            type="function",
                            function=SimpleNamespace(
                                name="lookup_b",
                                arguments='{"query":"B"}',
                            ),
                        ),
                    ],
                ),
            )
        ],
        usage=SimpleNamespace(
            prompt_tokens=20,
            completion_tokens=10,
            total_tokens=30,
        ),
    )


def _compatible_client(
    sdk_client: StubCompatibleClient | StubNonClosableClient,
    *,
    structured_output_mode: ChatStructuredOutputMode = ChatStructuredOutputMode.JSON_OBJECT,
    preserve_reasoning_content: bool = False,
    owns_client: bool = False,
) -> OpenAICompatibleChatModelClient:
    """使用固定能力配置构造待测通用适配器。"""
    return OpenAICompatibleChatModelClient(
        OpenAICompatibleChatConfig(
            provider="compatible-test",
            model="compatible-model",
            developer_role=ChatDeveloperRole.SYSTEM,
            structured_output_mode=structured_output_mode,
            max_tokens_field=ChatMaxTokensField.MAX_COMPLETION_TOKENS,
            preserve_reasoning_content=preserve_reasoning_content,
        ),
        client=sdk_client,
        owns_client=owns_client,
    )


def test_compatible_maps_json_object_developer_role_and_completion_limit() -> None:
    """JSON object 模式应注入 Schema，并映射角色与输出上限字段。"""

    async def scenario() -> None:
        sdk_client = StubCompatibleClient(_text_response())
        client = _compatible_client(sdk_client)

        await client.generate(
            ModelRequest(
                messages=(
                    ModelMessage(MessageRole.DEVELOPER, "遵守输出协议"),
                    ModelMessage(MessageRole.USER, "返回答案"),
                ),
                response_schema={
                    "type": "object",
                    "properties": {"answer": {"type": "string"}},
                    "required": ["answer"],
                },
                response_schema_name="answer_payload",
                max_output_tokens=256,
            )
        )

        parameters = sdk_client.completions.parameters[0]
        assert parameters["response_format"] == {"type": "json_object"}
        assert parameters["max_completion_tokens"] == 256
        assert "max_tokens" not in parameters
        messages = parameters["messages"]
        assert isinstance(messages, list)
        assert messages[0]["role"] == "system"
        assert "answer_payload" in messages[0]["content"]
        assert '"required": ["answer"]' in messages[0]["content"]
        assert messages[1] == {"role": "system", "content": "遵守输出协议"}
        assert messages[2] == {"role": "user", "content": "返回答案"}

    asyncio.run(scenario())


def test_compatible_maps_native_json_schema_response_format() -> None:
    """原生 JSON Schema 模式应保留名称、Schema 和 strict 标志。"""

    async def scenario() -> None:
        schema = {"type": "object", "properties": {"value": {"type": "integer"}}}
        sdk_client = StubCompatibleClient(_text_response())
        client = _compatible_client(
            sdk_client,
            structured_output_mode=ChatStructuredOutputMode.JSON_SCHEMA,
        )

        await client.generate(
            ModelRequest(
                messages=(ModelMessage(MessageRole.USER, "返回数字"),),
                response_schema=schema,
                response_schema_name="number_payload",
            )
        )

        assert sdk_client.completions.parameters[0]["response_format"] == {
            "type": "json_schema",
            "json_schema": {
                "name": "number_payload",
                "schema": schema,
                "strict": True,
            },
        }

    asyncio.run(scenario())


def test_compatible_preserves_multi_tool_continuation_order_without_repr_leak() -> None:
    """续轮应依次回传 assistant 与全部工具输出，并隐藏私有推理。"""

    async def scenario() -> None:
        private_reasoning = "sensitive-private-reasoning"
        sdk_client = StubCompatibleClient(
            _multi_tool_response(reasoning=private_reasoning),
            _text_response(),
        )
        client = _compatible_client(sdk_client, preserve_reasoning_content=True)

        first = await client.generate(
            ModelRequest(messages=(ModelMessage(MessageRole.USER, "并行查询"),))
        )
        assert tuple(call.call_id for call in first.tool_calls) == ("call-a", "call-b")
        assert first.continuation is not None
        assert private_reasoning not in repr(first)
        assert private_reasoning not in repr(first.continuation)

        continuation_request = ModelRequest(
            messages=(),
            tool_outputs=(
                ToolOutput("call-a", "证据 A"),
                ToolOutput("call-b", "证据 B", is_error=True),
            ),
            continuation=first.continuation,
        )
        assert private_reasoning not in repr(continuation_request)
        await client.generate(continuation_request)

        messages = sdk_client.completions.parameters[1]["messages"]
        assert isinstance(messages, list)
        assert messages[0] == {"role": "user", "content": "并行查询"}
        assert messages[1]["role"] == "assistant"
        assert messages[1]["reasoning_content"] == private_reasoning
        assert [call["id"] for call in messages[1]["tool_calls"]] == ["call-a", "call-b"]
        assert messages[2] == {
            "role": "tool",
            "tool_call_id": "call-a",
            "content": "证据 A",
        }
        assert messages[3] == {
            "role": "tool",
            "tool_call_id": "call-b",
            "content": '{"is_error": true, "content": "证据 B"}',
        }

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "tool_outputs",
    [
        (ToolOutput("call-a", "A"),),
        (ToolOutput("call-a", "A"), ToolOutput("call-c", "C")),
        (
            ToolOutput("call-a", "A-1"),
            ToolOutput("call-a", "A-2"),
            ToolOutput("call-b", "B"),
        ),
    ],
    ids=["missing", "unknown", "duplicate"],
)
def test_compatible_requires_exact_unique_tool_call_ids(
    tool_outputs: tuple[ToolOutput, ...],
) -> None:
    """工具续轮必须一次性提交全部且唯一的待处理 call id。"""

    async def scenario() -> None:
        sdk_client = StubCompatibleClient(_multi_tool_response())
        client = _compatible_client(sdk_client)
        first = await client.generate(
            ModelRequest(messages=(ModelMessage(MessageRole.USER, "查询"),))
        )

        with pytest.raises(ValueError, match="tool outputs must"):
            await client.generate(
                ModelRequest(
                    messages=(),
                    tool_outputs=tool_outputs,
                    continuation=first.continuation,
                )
            )

        assert len(sdk_client.completions.parameters) == 1

    asyncio.run(scenario())


def test_compatible_rejects_continuation_from_another_adapter_instance() -> None:
    """即使 provider/model 相同，续轮也不能跨适配器实例或租户复用。"""

    async def scenario() -> None:
        source_sdk = StubCompatibleClient(_multi_tool_response())
        target_sdk = StubCompatibleClient(_text_response())
        source = _compatible_client(source_sdk)
        target = _compatible_client(target_sdk)
        first = await source.generate(
            ModelRequest(messages=(ModelMessage(MessageRole.USER, "查询"),))
        )

        with pytest.raises(ValueError, match="another adapter transaction"):
            await target.generate(
                ModelRequest(
                    messages=(),
                    tool_outputs=(
                        ToolOutput("call-a", "A"),
                        ToolOutput("call-b", "B"),
                    ),
                    continuation=first.continuation,
                )
            )

        assert target_sdk.completions.parameters == []

    asyncio.run(scenario())


def test_compatible_normalizes_cached_and_reasoning_usage() -> None:
    """缓存命中、缓存未命中与推理 Token 应归一化到通用用量模型。"""

    async def scenario() -> None:
        usage = SimpleNamespace(
            prompt_tokens=100,
            completion_tokens=25,
            total_tokens=125,
            prompt_tokens_details=SimpleNamespace(cached_tokens=70),
            completion_tokens_details=SimpleNamespace(reasoning_tokens=11),
        )
        client = _compatible_client(StubCompatibleClient(_text_response(usage=usage)))

        response = await client.generate(
            ModelRequest(messages=(ModelMessage(MessageRole.USER, "统计用量"),))
        )

        assert response.usage.input_tokens == 100
        assert response.usage.output_tokens == 25
        assert response.usage.total_tokens == 125
        assert response.usage.cache_hit_tokens == 70
        assert response.usage.cache_miss_tokens == 30
        assert response.usage.reasoning_tokens == 11

    asyncio.run(scenario())


def test_compatible_parse_error_keeps_billable_usage_for_budget_settlement() -> None:
    """远端已生成响应时，非法工具参数仍应携带实际用量。"""

    async def scenario() -> None:
        response = _multi_tool_response()
        response.choices[0].message.tool_calls[0].function.arguments = "not-json"
        client = _compatible_client(StubCompatibleClient(response))

        with pytest.raises(ModelResponseParseError) as captured:
            await client.generate(ModelRequest(messages=(ModelMessage(MessageRole.USER, "调用"),)))

        assert captured.value.usage is not None
        assert captured.value.usage.total_tokens == 30

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("status_code", "expected_type"),
    [
        (401, ModelAuthenticationError),
        (402, ModelPaymentRequiredError),
        (429, ModelRateLimitError),
        (503, ModelServiceError),
        (400, ModelInvocationError),
    ],
)
def test_compatible_sanitizes_supplier_errors(
    status_code: int,
    expected_type: type[ModelInvocationError],
) -> None:
    """类型化异常不得包含供应商原始凭据文本或异常上下文。"""

    async def scenario() -> None:
        client = _compatible_client(StubCompatibleClient(SupplierStatusError(status_code)))

        with pytest.raises(expected_type) as captured:
            await client.generate(ModelRequest(messages=(ModelMessage(MessageRole.USER, "调用"),)))

        assert "sk-never-log-this" not in str(captured.value)
        assert "authorization" not in str(captured.value).lower()
        assert captured.value.__suppress_context__

    asyncio.run(scenario())


def test_compatible_closes_only_an_owned_client() -> None:
    """默认由调用方关闭客户端，显式转移所有权后才由适配器关闭。"""

    async def scenario() -> None:
        borrowed_sdk = StubCompatibleClient()
        borrowed = _compatible_client(borrowed_sdk)
        await borrowed.aclose()
        assert not borrowed_sdk.closed

        owned_sdk = StubCompatibleClient()
        owned = _compatible_client(owned_sdk, owns_client=True)
        await owned.aclose()
        assert owned_sdk.closed

    asyncio.run(scenario())


def test_compatible_rejects_ownership_without_async_close() -> None:
    """不可关闭的注入客户端不能把资源所有权交给适配器。"""
    with pytest.raises(TypeError, match="async close"):
        _compatible_client(StubNonClosableClient(), owns_client=True)


def test_compatible_maps_function_tools_without_enabling_vendor_strict_mode() -> None:
    """默认只发送本地校验 Schema，不擅自开启供应商 strict tools。"""

    async def scenario() -> None:
        sdk_client = StubCompatibleClient(_text_response())
        client = _compatible_client(sdk_client)
        await client.generate(
            ModelRequest(
                messages=(ModelMessage(MessageRole.USER, "查询"),),
                tools=(
                    ToolDefinition(
                        name="lookup",
                        description="查询证据",
                        parameters={
                            "type": "object",
                            "properties": {"query": {"type": "string"}},
                        },
                    ),
                ),
            )
        )

        tools = sdk_client.completions.parameters[0]["tools"]
        assert isinstance(tools, list)
        assert tools[0]["function"]["name"] == "lookup"
        assert "strict" not in tools[0]["function"]

    asyncio.run(scenario())
