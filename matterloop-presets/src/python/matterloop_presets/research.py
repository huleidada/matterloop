"""只读资料、HTTPS 白名单和引用证据研究预设。"""

from pathlib import Path

from matterloop_core import LocalEventPublisher
from matterloop_memory import InMemoryCheckpointStore
from matterloop_models import ModelClient
from matterloop_policies import (
    AllowAllApproval,
    PermissionRule,
    RuleBasedPermissionPolicy,
)
from matterloop_runtime import LocalRuntime
from matterloop_tools import FileSystemTool, HttpTool, PermissionDecision, ToolRegistry

from matterloop_presets._assembly import _assemble_runtime
from matterloop_presets.config import ResearchPresetConfig
from matterloop_presets.runtime import PresetRuntime


def build_research_runtime(
    model: ModelClient,
    workspace: str | Path,
    config: ResearchPresetConfig,
) -> PresetRuntime:
    """构建只读文件、HTTPS 白名单和引用验证研究运行时。

    Args:
        model: 规划、执行和验证共享的模型客户端。
        workspace: 本地只读资料的工作区根目录。
        config: 包含显式 HTTPS 主机白名单的不可变配置。

    Returns:
        仅允许受限资料读取并执行引用验证的异步运行时。
    """
    authorizer = RuleBasedPermissionPolicy(
        rules=(
            PermissionRule(
                "filesystem",
                ("read", "list", "exists", "stat"),
                PermissionDecision.ALLOW,
            ),
            PermissionRule("http", ("invoke",), PermissionDecision.ALLOW),
        )
    )
    tools = ToolRegistry(
        (
            FileSystemTool(
                workspace,
                allow_write=False,
                max_read_bytes=config.max_read_bytes,
            ),
            HttpTool(
                allowed_hosts=config.allowed_hosts,
                allowed_methods=frozenset({"GET"}),
                require_https=True,
                follow_redirects=False,
                max_timeout_seconds=config.max_http_timeout_seconds,
                max_response_bytes=config.max_response_bytes,
            ),
        ),
        authorizer=authorizer,
    )
    return _assemble_runtime(
        model=model,
        config=config,
        checkpoint_store=InMemoryCheckpointStore(),
        events=LocalEventPublisher(),
        approval_gate=AllowAllApproval(),
        tool_registries={"default": tools},
        executor_tools={"default": ("filesystem", "http")},
        require_citation=config.require_citation,
    )


def build_research_local_runtime(
    model: ModelClient,
    workspace: str | Path,
    config: ResearchPresetConfig,
) -> LocalRuntime:
    """构建研究预设的同步专用事件循环门面。

    Args:
        model: 规划、执行和验证共享的模型客户端。
        workspace: 本地只读资料的工作区根目录。
        config: 包含显式 HTTPS 主机白名单的不可变配置。

    Returns:
        可通过上下文管理器或 ``close()`` 关闭的同步门面。
    """
    return LocalRuntime(build_research_runtime(model, workspace, config))


__all__ = ["build_research_local_runtime", "build_research_runtime"]
