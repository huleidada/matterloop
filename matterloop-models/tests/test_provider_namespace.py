"""供应商子包公共边界测试。"""

from __future__ import annotations

import importlib

import matterloop_models
import matterloop_models.providers as provider_namespace
import pytest
from matterloop_models import ModelClient, ModelRegistry
from matterloop_models.providers import (
    DeepSeekChatModelClient,
    MiniMaxChatClient,
    MiniMaxChatContinuation,
    MiniMaxChatModelClient,
    MiniMaxModelConfig,
    OpenAICompatibleChatModelClient,
    OpenAIModelClient,
    QwenChatModelClient,
    ZhipuChatModelClient,
)


def test_provider_types_are_exported_only_from_provider_namespace() -> None:
    """根命名空间保持中立，供应商类型由子包稳定导出。"""
    assert ModelClient is not None
    assert ModelRegistry is not None
    assert DeepSeekChatModelClient is not None
    assert MiniMaxChatClient is not None
    assert MiniMaxChatContinuation is not None
    assert MiniMaxChatModelClient is not None
    assert MiniMaxModelConfig is not None
    assert OpenAICompatibleChatModelClient is not None
    assert OpenAIModelClient is not None
    assert QwenChatModelClient is not None
    assert ZhipuChatModelClient is not None
    assert not hasattr(matterloop_models, "DeepSeekChatModelClient")
    assert not hasattr(matterloop_models, "MiniMaxChatModelClient")
    assert not hasattr(matterloop_models, "MiniMaxModelConfig")
    assert not hasattr(matterloop_models, "OpenAIModelClient")
    assert {
        "MiniMaxChatClient",
        "MiniMaxChatContinuation",
        "MiniMaxChatModelClient",
        "MiniMaxModelConfig",
    } <= set(provider_namespace.__all__)


@pytest.mark.parametrize(
    "module_name",
    ["compatible", "deepseek", "minimax", "openai", "qwen", "zhipu"],
)
def test_provider_modules_do_not_leak_into_root_namespace(module_name: str) -> None:
    """供应商模块只存在于 providers 子包，不提供根层隐式路径。"""
    with pytest.raises(ModuleNotFoundError):
        importlib.import_module(f"matterloop_models.{module_name}")
