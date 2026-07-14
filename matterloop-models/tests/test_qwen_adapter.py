"""千问 Chat Completions 适配器的纯离线参数与能力测试。"""

from __future__ import annotations

import asyncio
from collections import deque
from types import SimpleNamespace

import pytest
from matterloop_models.base import (
    MessageRole,
    ModelMessage,
    ModelRequest,
    ToolChoice,
    ToolDefinition,
)
from matterloop_models.errors import ModelCapabilityError, ModelRateLimitError
from matterloop_models.providers import (
    QwenChatModelClient,
    QwenModelConfig,
    QwenThinkingMode,
)


class StubCompletions:
    """记录千问 SDK 调用并返回预置离线响应。"""

    def __init__(self, *results: object) -> None:
        self._results = deque(results)
        self.parameters: list[dict[str, object]] = []

    async def create(self, **kwargs: object) -> object:
        """模拟异步 Chat Completions 请求。"""
        self.parameters.append(kwargs)
        result = self._results.popleft()
        if isinstance(result, Exception):
            raise result
        return result


class StubQwenClient:
    """提供调用方构造并注入的最小千问客户端。"""

    def __init__(self, *results: object) -> None:
        self.completions = StubCompletions(*results)
        self.chat = SimpleNamespace(completions=self.completions)
        self.closed = False

    async def close(self) -> None:
        """记录资源所有权是否转移给适配器。"""
        self.closed = True


class SupplierRateLimitError(RuntimeError):
    """模拟正文中意外携带凭据的千问限流异常。"""

    def __init__(self) -> None:
        super().__init__("Authorization Bearer sk-sensitive-qwen")
        self.status_code = 429


def _response() -> object:
    """构造包含千问常见 usage 明细的离线响应。"""
    return SimpleNamespace(
        id="chatcmpl-qwen",
        model="qwen-plus",
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
        usage=SimpleNamespace(
            prompt_tokens=60,
            completion_tokens=20,
            total_tokens=80,
            prompt_tokens_details=SimpleNamespace(cached_tokens=35),
            completion_tokens_details=SimpleNamespace(reasoning_tokens=7),
        ),
    )


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


def test_qwen_enabled_thinking_maps_vendor_parameters_and_parallel_tools() -> None:
    """显式思考模式应通过 extra_body 发送预算、保留和并行工具参数。"""

    async def scenario() -> None:
        sdk_client = StubQwenClient(_response())
        client = QwenChatModelClient(
            QwenModelConfig(
                model="qwen-plus",
                thinking_mode=QwenThinkingMode.ENABLED,
                thinking_budget=1024,
                preserve_thinking=True,
                parallel_tool_calls=True,
            ),
            client=sdk_client,
        )

        await client.generate(
            ModelRequest(
                messages=(ModelMessage(MessageRole.USER, "查询证据"),),
                tools=(_lookup_tool(),),
                max_output_tokens=512,
            )
        )

        parameters = sdk_client.completions.parameters[0]
        assert parameters["extra_body"] == {
            "enable_thinking": True,
            "thinking_budget": 1024,
            "preserve_thinking": True,
        }
        assert parameters["parallel_tool_calls"] is True
        assert parameters["max_completion_tokens"] == 512
        assert "max_tokens" not in parameters
        tools = parameters["tools"]
        assert isinstance(tools, list)
        assert "strict" not in tools[0]["function"]

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("mode", "expected_extra_body"),
    [
        (QwenThinkingMode.DEFAULT, None),
        (QwenThinkingMode.DISABLED, {"enable_thinking": False}),
        (QwenThinkingMode.ENABLED, {"enable_thinking": True}),
    ],
)
def test_qwen_maps_explicit_and_default_thinking_modes(
    mode: QwenThinkingMode,
    expected_extra_body: dict[str, object] | None,
) -> None:
    """DEFAULT 不覆盖模型行为，显式模式才发送 enable_thinking。"""

    async def scenario() -> None:
        sdk_client = StubQwenClient(_response())
        client = QwenChatModelClient(
            QwenModelConfig(model="qwen-plus", thinking_mode=mode),
            client=sdk_client,
        )
        await client.generate(ModelRequest(messages=(ModelMessage(MessageRole.USER, "回答"),)))

        parameters = sdk_client.completions.parameters[0]
        if expected_extra_body is None:
            assert "extra_body" not in parameters
        else:
            assert parameters["extra_body"] == expected_extra_body

    asyncio.run(scenario())


def test_qwen_default_mode_disables_thinking_for_structured_output() -> None:
    """DEFAULT 遇到 JSON object 请求时应自动关闭思考并注入 Schema。"""

    async def scenario() -> None:
        sdk_client = StubQwenClient(_response())
        client = QwenChatModelClient(
            QwenModelConfig(model="qwen-plus"),
            client=sdk_client,
        )
        await client.generate(
            ModelRequest(
                messages=(
                    ModelMessage(MessageRole.DEVELOPER, "严格遵守格式"),
                    ModelMessage(MessageRole.USER, "回答"),
                ),
                response_schema={
                    "type": "object",
                    "properties": {"answer": {"type": "string"}},
                },
                response_schema_name="qwen_answer",
                max_output_tokens=200,
            )
        )

        parameters = sdk_client.completions.parameters[0]
        assert parameters["extra_body"] == {"enable_thinking": False}
        assert parameters["response_format"] == {"type": "json_object"}
        assert parameters["max_completion_tokens"] == 200
        messages = parameters["messages"]
        assert isinstance(messages, list)
        assert messages[0]["role"] == "system"
        assert "qwen_answer" in messages[0]["content"]
        assert messages[1] == {"role": "system", "content": "严格遵守格式"}

    asyncio.run(scenario())


def test_qwen_rejects_structured_output_while_thinking_is_enabled() -> None:
    """千问思考模式与 JSON object 结构化输出不能出现在同一请求。"""

    async def scenario() -> None:
        sdk_client = StubQwenClient()
        client = QwenChatModelClient(
            QwenModelConfig(
                model="qwen-plus",
                thinking_mode=QwenThinkingMode.ENABLED,
            ),
            client=sdk_client,
        )

        with pytest.raises(ModelCapabilityError, match="thinking is enabled"):
            await client.generate(
                ModelRequest(
                    messages=(ModelMessage(MessageRole.USER, "结构化回答"),),
                    response_schema={"type": "object"},
                )
            )

        assert sdk_client.completions.parameters == []

    asyncio.run(scenario())


def test_qwen_rejects_required_tool_choice_while_thinking_is_enabled() -> None:
    """显式思考模式不能与千问 REQUIRED 工具选择组合。"""

    async def scenario() -> None:
        sdk_client = StubQwenClient()
        client = QwenChatModelClient(
            QwenModelConfig(
                model="qwen-plus",
                thinking_mode=QwenThinkingMode.ENABLED,
            ),
            client=sdk_client,
        )

        with pytest.raises(ModelCapabilityError, match="required tool choice"):
            await client.generate(
                ModelRequest(
                    messages=(ModelMessage(MessageRole.USER, "调用工具"),),
                    tools=(_lookup_tool(),),
                    tool_choice=ToolChoice.REQUIRED,
                )
            )

        assert sdk_client.completions.parameters == []

    asyncio.run(scenario())


def test_qwen_normalizes_cache_and_reasoning_usage() -> None:
    """千问缓存和思考用量应落入统一 TokenUsage 字段。"""

    async def scenario() -> None:
        client = QwenChatModelClient(
            QwenModelConfig(model="qwen-plus"),
            client=StubQwenClient(_response()),
        )
        response = await client.generate(
            ModelRequest(messages=(ModelMessage(MessageRole.USER, "统计"),))
        )

        assert response.usage.input_tokens == 60
        assert response.usage.output_tokens == 20
        assert response.usage.total_tokens == 80
        assert response.usage.cache_hit_tokens == 35
        assert response.usage.cache_miss_tokens == 25
        assert response.usage.reasoning_tokens == 7

    asyncio.run(scenario())


def test_qwen_sanitizes_supplier_exception_text() -> None:
    """继承的通用错误映射不得泄露千问客户端异常正文。"""

    async def scenario() -> None:
        client = QwenChatModelClient(
            QwenModelConfig(model="qwen-plus"),
            client=StubQwenClient(SupplierRateLimitError()),
        )

        with pytest.raises(ModelRateLimitError) as captured:
            await client.generate(ModelRequest(messages=(ModelMessage(MessageRole.USER, "调用"),)))

        assert "sk-sensitive-qwen" not in str(captured.value)
        assert "authorization" not in str(captured.value).lower()
        assert captured.value.__suppress_context__

    asyncio.run(scenario())


def test_qwen_honors_injected_client_ownership() -> None:
    """千问适配器只关闭显式交由其管理的客户端。"""

    async def scenario() -> None:
        borrowed_sdk = StubQwenClient()
        borrowed = QwenChatModelClient(
            QwenModelConfig(model="qwen-plus"),
            client=borrowed_sdk,
        )
        await borrowed.aclose()
        assert not borrowed_sdk.closed

        owned_sdk = StubQwenClient()
        owned = QwenChatModelClient(
            QwenModelConfig(model="qwen-plus"),
            client=owned_sdk,
            owns_client=True,
        )
        await owned.aclose()
        assert owned_sdk.closed

    asyncio.run(scenario())
