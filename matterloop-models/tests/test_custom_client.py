"""可调用模型客户端测试。"""

from __future__ import annotations

import asyncio

import pytest
from matterloop_models import MessageRole, ModelMessage, ModelRequest, ModelResponse
from matterloop_models.capabilities import ModelDescriptor
from matterloop_models.custom import CallableModelClient


def test_callable_model_client_delegates_and_closes_once() -> None:
    """自定义异步函数可直接适配，关闭回调幂等执行。"""

    async def scenario() -> None:
        requests: list[ModelRequest] = []
        close_calls = 0

        async def generate(request: ModelRequest) -> ModelResponse:
            requests.append(request)
            return ModelResponse(output_text="custom-result")

        async def close() -> None:
            nonlocal close_calls
            close_calls += 1

        descriptor = ModelDescriptor(provider="custom", model="callback-model")
        client = CallableModelClient(
            generate,
            close_callback=close,
            descriptor=descriptor,
        )
        request = ModelRequest(messages=(ModelMessage(MessageRole.USER, "hello"),))

        response = await client.generate(request)
        await client.aclose()
        await client.aclose()

        assert response.output_text == "custom-result"
        assert requests == [request]
        assert client.descriptor is descriptor
        assert close_calls == 1
        with pytest.raises(RuntimeError, match="closed"):
            await client.generate(request)

    asyncio.run(scenario())


def test_callable_model_client_does_not_require_close_callback() -> None:
    """不持有资源的自定义客户端无需伪造 close 实现。"""

    async def scenario() -> None:
        async def generate(request: ModelRequest) -> ModelResponse:
            del request
            return ModelResponse(output_text="ok")

        client = CallableModelClient(generate)
        await client.aclose()
        await client.aclose()

    asyncio.run(scenario())


def test_callable_model_client_rejects_invalid_response_type() -> None:
    """自定义回调仍必须遵循通用响应边界。"""

    async def scenario() -> None:
        async def invalid_generate(request: ModelRequest) -> ModelResponse:
            del request
            return "invalid"  # type: ignore[return-value]

        client = CallableModelClient(invalid_generate)
        request = ModelRequest(messages=(ModelMessage(MessageRole.USER, "hello"),))

        with pytest.raises(TypeError, match="ModelResponse"):
            await client.generate(request)

    asyncio.run(scenario())
