"""MatterLoop 预设使用的不可变配置对象。"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType

from matterloop_policies import RetryConfig


@dataclass(frozen=True, slots=True)
class AgentPresetConfig:
    """配置所有预设共享的模型 Agent 与收敛边界。

    Args:
        model_name: 模型注册表中的稳定名称。
        max_plan_steps: 规划器单轮最多返回的步骤数。
        max_tool_rounds: Worker 单步骤最多执行的工具反馈轮数。
        pass_score: Verifier 判定通过的最低分数。
        max_identical_feedback: 相同失败反馈触发停止前允许的次数。
        retry: 组件异常的指数退避配置。
    """

    model_name: str = "default"
    max_plan_steps: int = 20
    max_tool_rounds: int = 8
    pass_score: float = 80
    max_identical_feedback: int = 2
    retry: RetryConfig = field(default_factory=RetryConfig)

    def __post_init__(self) -> None:
        """校验会影响模型选择和闭环收敛的配置。"""
        if not self.model_name.strip():
            raise ValueError("model_name must not be empty")
        if self.max_plan_steps < 1:
            raise ValueError("max_plan_steps must be at least 1")
        if self.max_tool_rounds < 1:
            raise ValueError("max_tool_rounds must be at least 1")
        if not 0 <= self.pass_score <= 100:
            raise ValueError("pass_score must be between 0 and 100")
        if self.max_identical_feedback < 1:
            raise ValueError("max_identical_feedback must be at least 1")


@dataclass(frozen=True, slots=True)
class MinimalPresetConfig(AgentPresetConfig):
    """最小预设配置；不会启用任何外部工具。"""


@dataclass(frozen=True, slots=True)
class CodingPresetConfig(AgentPresetConfig):
    """受限编码预设配置。

    Args:
        privileged_executor: 唯一可以看到写文件与 Shell 工具的执行器名称。
        allowed_commands: Shell 工具允许执行的裸程序名。
        shell_environment: Shell 子进程使用的显式基础环境；默认不继承宿主环境。
        max_read_bytes: 单次文件读取上限。
        max_write_bytes: 单次文件写入上限。
        max_shell_timeout_seconds: 单次命令最大运行秒数。
        max_shell_output_bytes: 单次命令输出上限。
    """

    privileged_executor: str = "coding"
    allowed_commands: frozenset[str] = frozenset({"pytest", "ruff"})
    shell_environment: Mapping[str, str] = field(default_factory=dict, repr=False)
    max_read_bytes: int = 1_000_000
    max_write_bytes: int = 1_000_000
    max_shell_timeout_seconds: float = 60
    max_shell_output_bytes: int = 1_000_000

    def __post_init__(self) -> None:
        """校验编码工具的权限分区和资源上限。"""
        AgentPresetConfig.__post_init__(self)
        if not self.privileged_executor.strip() or self.privileged_executor == "default":
            raise ValueError("privileged_executor must be non-empty and different from default")
        if not self.allowed_commands or any(
            not command.strip() for command in self.allowed_commands
        ):
            raise ValueError("allowed_commands must contain non-empty program names")
        copied_environment: dict[str, str] = {}
        for key, value in self.shell_environment.items():
            if not isinstance(key, str) or not isinstance(value, str):
                raise ValueError("shell_environment keys and values must be strings")
            if not key or "=" in key or "\x00" in key or "\x00" in value:
                raise ValueError("shell_environment contains an invalid key or NUL byte")
            copied_environment[key] = value
        object.__setattr__(
            self,
            "shell_environment",
            MappingProxyType(copied_environment),
        )
        if min(self.max_read_bytes, self.max_write_bytes, self.max_shell_output_bytes) < 1:
            raise ValueError("coding byte limits must be positive")
        if self.max_shell_timeout_seconds <= 0:
            raise ValueError("max_shell_timeout_seconds must be greater than 0")


@dataclass(frozen=True, slots=True)
class ResearchPresetConfig(AgentPresetConfig):
    """只读研究预设配置。

    Args:
        allowed_hosts: HTTP 工具可以精确访问的 HTTPS 主机集合。
        max_read_bytes: 单次本地资料读取上限。
        max_response_bytes: 单次 HTTP 响应体上限。
        max_http_timeout_seconds: 单次 HTTP 请求最大秒数。
        require_citation: 验证通过时是否必须至少包含一个 URL 或制品引用。
    """

    allowed_hosts: frozenset[str] = frozenset()
    max_read_bytes: int = 1_000_000
    max_response_bytes: int = 2_000_000
    max_http_timeout_seconds: float = 20
    require_citation: bool = True

    def __post_init__(self) -> None:
        """强制显式 HTTPS 主机白名单和有限资源边界。"""
        AgentPresetConfig.__post_init__(self)
        if not self.allowed_hosts or any(not host.strip() for host in self.allowed_hosts):
            raise ValueError("allowed_hosts must contain at least one non-empty host")
        if min(self.max_read_bytes, self.max_response_bytes) < 1:
            raise ValueError("research byte limits must be positive")
        if self.max_http_timeout_seconds <= 0:
            raise ValueError("max_http_timeout_seconds must be greater than 0")


@dataclass(frozen=True, slots=True)
class ProductionPresetConfig(AgentPresetConfig):
    """生产 worker 的模型 Agent 配置；默认不启用危险工具。"""


__all__ = [
    "AgentPresetConfig",
    "CodingPresetConfig",
    "MinimalPresetConfig",
    "ProductionPresetConfig",
    "ResearchPresetConfig",
]
