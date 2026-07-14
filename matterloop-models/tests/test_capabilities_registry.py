"""模型能力描述与安全热替换注册表测试。"""

from __future__ import annotations

import asyncio

import pytest
from matterloop_models import (
    CallableModelClient,
    FakeModelClient,
    ModelNotFoundError,
    ModelRequest,
    ModelResponse,
)
from matterloop_models.capabilities import (
    CapabilityStatus,
    ModelCapabilities,
    ModelDescriptor,
    ModelFeature,
    ModelRequirements,
)
from matterloop_models.registry import ModelRegistry


def test_capabilities_distinguish_unknown_from_unsupported() -> None:
    """未声明能力不得被当成明确不支持。"""
    capabilities = ModelCapabilities(
        supported=frozenset({ModelFeature.TOOL_CALLING}),
        unsupported=frozenset({ModelFeature.JSON_SCHEMA_OUTPUT}),
    )

    assert capabilities.status(ModelFeature.TOOL_CALLING) is CapabilityStatus.SUPPORTED
    assert capabilities.status(ModelFeature.JSON_SCHEMA_OUTPUT) is CapabilityStatus.UNSUPPORTED
    assert capabilities.status(ModelFeature.REASONING) is CapabilityStatus.UNKNOWN
    assert not capabilities.supports(ModelFeature.REASONING)


def test_requirements_reject_unknown_by_default_and_can_allow_it() -> None:
    """自动匹配默认快速失败，显式配置后才接受未知能力。"""
    descriptor = ModelDescriptor(
        provider="custom",
        model="private-model",
        capabilities=ModelCapabilities(
            supported=frozenset({ModelFeature.TEXT_GENERATION}),
        ),
    )
    strict = ModelRequirements(
        required_features=frozenset({ModelFeature.TEXT_GENERATION, ModelFeature.TOOL_CALLING}),
        provider="custom",
    )
    permissive = ModelRequirements(
        required_features=strict.required_features,
        provider="custom",
        allow_unknown=True,
    )

    assert not strict.matches(descriptor)
    assert permissive.matches(descriptor)
    assert not ModelRequirements(provider="other", allow_unknown=True).matches(descriptor)


def test_capabilities_reject_conflicting_declarations() -> None:
    """同一能力不能同时标记为支持与不支持。"""
    with pytest.raises(ValueError, match="conflicting"):
        ModelCapabilities(
            supported=frozenset({ModelFeature.TOOL_CALLING}),
            unsupported=frozenset({ModelFeature.TOOL_CALLING}),
        )


def test_registry_stores_descriptor_without_requiring_it() -> None:
    """新描述是可选能力，旧注册方式仍返回 None。"""
    registry = ModelRegistry()
    descriptor = ModelDescriptor(provider="deepseek", model="deepseek-v4-flash")
    registry.register("described", FakeModelClient(), descriptor=descriptor)
    registry.register("legacy", FakeModelClient())

    assert registry.describe("described") is descriptor
    assert registry.describe("legacy") is None


def test_registry_infers_descriptor_from_provider_or_custom_adapter() -> None:
    """适配器公开 descriptor 后，调用方无需在注册时重复传入。"""

    async def generate(_request: ModelRequest) -> ModelResponse:
        return ModelResponse(output_text="ok")

    descriptor = ModelDescriptor(provider="custom", model="private-model")
    client = CallableModelClient(generate, descriptor=descriptor)
    registry = ModelRegistry()

    registry.register("custom", client)

    assert registry.describe("custom") is descriptor


def test_swap_pins_old_client_until_every_lease_is_released() -> None:
    """旧事务完成前退役句柄不得报告已排空。"""

    async def scenario() -> None:
        registry = ModelRegistry()
        original = FakeModelClient()
        replacement = FakeModelClient()
        original_descriptor = ModelDescriptor(provider="custom", model="old")
        replacement_descriptor = ModelDescriptor(provider="custom", model="new")
        registry.register("worker", original, descriptor=original_descriptor)
        lease = registry.acquire("worker")

        async with lease as pinned:
            retirement = registry.swap(
                "worker",
                replacement,
                descriptor=replacement_descriptor,
            )
            waiting = asyncio.create_task(retirement.wait_drained())
            await asyncio.sleep(0)

            assert pinned is original
            assert registry.get("worker") is replacement
            assert registry.describe("worker") is replacement_descriptor
            assert retirement.client is original
            assert retirement.descriptor is original_descriptor
            assert not retirement.is_drained
            assert not waiting.done()

        assert await asyncio.wait_for(waiting, timeout=1) is original
        assert retirement.is_drained

    asyncio.run(scenario())


def test_retire_removes_name_and_waits_for_unentered_lease_release() -> None:
    """调用方可显式释放尚未进入上下文的租约。"""

    async def scenario() -> None:
        registry = ModelRegistry()
        client = FakeModelClient()
        registry.register("worker", client)
        lease = registry.acquire("worker")

        retirement = registry.retire("worker")

        with pytest.raises(ModelNotFoundError):
            registry.get("worker")
        assert not retirement.is_drained

        lease.release()

        assert await asyncio.wait_for(retirement.wait_drained(), timeout=1) is client
        assert retirement.is_drained

    asyncio.run(scenario())


def test_retire_without_active_lease_is_immediately_drained() -> None:
    """没有旧调用时退役操作无需异步等待。"""

    async def scenario() -> None:
        registry = ModelRegistry()
        client = FakeModelClient()
        registry.register("worker", client)

        retirement = registry.retire("worker")

        assert retirement.is_drained
        assert await retirement.wait_drained() is client

    asyncio.run(scenario())
