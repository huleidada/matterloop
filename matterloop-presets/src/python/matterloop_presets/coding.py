"""工作区读写隔离且命令受限的编码运行时预设。"""

from pathlib import Path

from matterloop_core import ApprovalDecision, ApprovalGate, LocalEventPublisher
from matterloop_memory import InMemoryCheckpointStore
from matterloop_models import ModelClient
from matterloop_policies import (
    PermissionRule,
    RuleBasedApprovalGate,
    RuleBasedPermissionPolicy,
)
from matterloop_runtime import LocalRuntime
from matterloop_tools import FileSystemTool, PermissionDecision, ShellTool, ToolRegistry

from matterloop_presets._assembly import _assemble_runtime
from matterloop_presets.config import CodingPresetConfig
from matterloop_presets.runtime import PresetRuntime


def build_coding_runtime(
    model: ModelClient,
    workspace: str | Path,
    config: CodingPresetConfig | None = None,
    *,
    approval_gate: ApprovalGate | None = None,
) -> PresetRuntime:
    """构建读写权限隔离且命令受白名单限制的编码运行时。

    默认执行器只能读取工作区。高权限执行器同时拥有写文件和受限 Shell；规划结果会被
    强制标记为需要审批，默认审批门返回 deferred，调用方必须注入实际审批实现才能执行。

    Args:
        model: 规划、执行和验证共享的模型客户端。
        workspace: 文件和命令允许访问的工作区根目录。
        config: 可选不可变配置。
        approval_gate: 高权限步骤使用的审批实现；缺省时安全暂停。

    Returns:
        拥有只读与高权限两个隔离执行器的异步运行时。
    """
    actual_config = config or CodingPresetConfig()
    read_authorizer = RuleBasedPermissionPolicy(
        rules=(
            PermissionRule(
                "filesystem",
                ("read", "list", "exists", "stat"),
                PermissionDecision.ALLOW,
            ),
        )
    )
    privileged_authorizer = RuleBasedPermissionPolicy(
        rules=(
            PermissionRule("filesystem", ("*",), PermissionDecision.ALLOW),
            PermissionRule("shell", ("*",), PermissionDecision.ALLOW),
        )
    )
    read_tools = ToolRegistry(
        (
            FileSystemTool(
                workspace,
                allow_write=False,
                max_read_bytes=actual_config.max_read_bytes,
            ),
        ),
        authorizer=read_authorizer,
    )
    privileged_tools = ToolRegistry(
        (
            FileSystemTool(
                workspace,
                allow_write=True,
                max_read_bytes=actual_config.max_read_bytes,
                max_write_bytes=actual_config.max_write_bytes,
            ),
            ShellTool(
                workspace,
                allowed_commands=actual_config.allowed_commands,
                base_environment=actual_config.shell_environment,
                max_timeout_seconds=actual_config.max_shell_timeout_seconds,
                max_output_bytes=actual_config.max_shell_output_bytes,
            ),
        ),
        authorizer=privileged_authorizer,
    )
    return _assemble_runtime(
        model=model,
        config=actual_config,
        checkpoint_store=InMemoryCheckpointStore(),
        events=LocalEventPublisher(),
        approval_gate=approval_gate or RuleBasedApprovalGate(default=ApprovalDecision.DEFERRED),
        tool_registries={
            "default": read_tools,
            actual_config.privileged_executor: privileged_tools,
        },
        executor_tools={
            "default": ("filesystem",),
            actual_config.privileged_executor: ("filesystem", "shell"),
        },
        privileged_executors=frozenset({actual_config.privileged_executor}),
    )


def build_coding_local_runtime(
    model: ModelClient,
    workspace: str | Path,
    config: CodingPresetConfig | None = None,
    *,
    approval_gate: ApprovalGate | None = None,
) -> LocalRuntime:
    """构建编码预设的同步专用事件循环门面。

    Args:
        model: 规划、执行和验证共享的模型客户端。
        workspace: 文件和命令允许访问的工作区根目录。
        config: 可选不可变配置。
        approval_gate: 高权限步骤使用的审批实现。

    Returns:
        可通过上下文管理器或 ``close()`` 关闭的同步门面。
    """
    return LocalRuntime(
        build_coding_runtime(
            model,
            workspace,
            config,
            approval_gate=approval_gate,
        )
    )


__all__ = ["build_coding_local_runtime", "build_coding_runtime"]
