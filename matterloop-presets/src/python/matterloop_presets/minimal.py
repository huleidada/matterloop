"""不启用外部工具的最小运行时预设。"""

from matterloop_core import LocalEventPublisher
from matterloop_memory import InMemoryCheckpointStore
from matterloop_models import ModelClient
from matterloop_policies import AllowAllApproval
from matterloop_runtime import LocalRuntime
from matterloop_tools import ToolRegistry

from matterloop_presets._assembly import _assemble_runtime
from matterloop_presets.config import MinimalPresetConfig
from matterloop_presets.runtime import PresetRuntime


def build_minimal_runtime(
    model: ModelClient,
    config: MinimalPresetConfig | None = None,
) -> PresetRuntime:
    """构建不启用外部工具的最小异步运行时。

    Args:
        model: 规划、执行和验证共享的可热替换模型客户端。
        config: 可选不可变配置。

    Returns:
        使用内存检查点、基础 Agent 和空工具注册表的异步运行时。
    """
    actual_config = config or MinimalPresetConfig()
    tools = ToolRegistry()
    return _assemble_runtime(
        model=model,
        config=actual_config,
        checkpoint_store=InMemoryCheckpointStore(),
        events=LocalEventPublisher(),
        approval_gate=AllowAllApproval(),
        tool_registries={"default": tools},
        executor_tools={"default": ()},
    )


def build_minimal_local_runtime(
    model: ModelClient,
    config: MinimalPresetConfig | None = None,
) -> LocalRuntime:
    """构建最小预设的同步专用事件循环门面。

    Args:
        model: 规划、执行和验证共享的模型客户端。
        config: 可选不可变配置。

    Returns:
        可通过上下文管理器或 ``close()`` 关闭的同步门面。
    """
    return LocalRuntime(build_minimal_runtime(model, config))


__all__ = ["build_minimal_local_runtime", "build_minimal_runtime"]
