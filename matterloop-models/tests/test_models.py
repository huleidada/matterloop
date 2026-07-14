"""模型值对象、注册表和假客户端测试。"""

from __future__ import annotations

import asyncio

import pytest
from matterloop_models import (
    FakeModelClient,
    FakeModelExhaustedError,
    MessageRole,
    ModelAlreadyRegisteredError,
    ModelMessage,
    ModelRegistry,
    ModelRequest,
    ModelResponse,
    TokenUsage,
)


def test_fake_model_records_requests_and_returns_in_order() -> None:
    async def scenario() -> None:
        client = FakeModelClient(
            [ModelResponse(output_text="first"), ModelResponse(output_text="second")]
        )
        request = ModelRequest(messages=(ModelMessage(MessageRole.USER, "hello"),))

        assert (await client.generate(request)).output_text == "first"
        assert (await client.generate(request)).output_text == "second"
        assert client.requests == (request, request)

        with pytest.raises(FakeModelExhaustedError):
            await client.generate(request)

    asyncio.run(scenario())


def test_model_registry_supports_explicit_hot_replacement() -> None:
    registry = ModelRegistry()
    original = FakeModelClient()
    replacement = FakeModelClient()
    registry.register("worker", original)

    with pytest.raises(ModelAlreadyRegisteredError):
        registry.register("worker", replacement)

    registry.register("worker", replacement, replace=True)
    assert registry.get("worker") is replacement


def test_model_registry_lease_pins_client_during_hot_replacement() -> None:
    async def scenario() -> None:
        registry = ModelRegistry()
        original = FakeModelClient()
        replacement = FakeModelClient()
        registry.register("worker", original)

        old_transaction = registry.acquire("worker")
        registry.register("worker", replacement, replace=True)

        async with old_transaction as pinned:
            assert pinned is original
            assert registry.get("worker") is replacement
        async with registry.acquire("worker") as current:
            assert current is replacement

    asyncio.run(scenario())


def test_request_freezes_metadata() -> None:
    metadata: dict[str, object] = {"trace_id": "before"}
    request = ModelRequest(
        messages=(ModelMessage(MessageRole.USER, "hello"),),
        metadata=metadata,
    )
    metadata["trace_id"] = "after"

    assert request.metadata["trace_id"] == "before"


def test_request_normalizes_usage_scopes_and_rejects_duplicates() -> None:
    message = ModelMessage(MessageRole.USER, "hello")
    request = ModelRequest(messages=(message,), usage_scopes=(" team ", "task:1"))

    assert request.usage_scopes == ("team", "task:1")
    with pytest.raises(ValueError, match="duplicates"):
        ModelRequest(messages=(message,), usage_scopes=("team", "team"))


def test_token_usage_accepts_cache_and_reasoning_dimensions() -> None:
    usage = TokenUsage(
        input_tokens=10,
        output_tokens=5,
        total_tokens=15,
        cache_hit_tokens=7,
        cache_miss_tokens=3,
        reasoning_tokens=2,
    )

    assert usage.cache_hit_tokens == 7
    assert usage.cache_miss_tokens == 3
    assert usage.reasoning_tokens == 2
