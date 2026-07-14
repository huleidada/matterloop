"""定义 Agent 组件的不可变配置对象。"""

from __future__ import annotations

from dataclasses import dataclass


def _validate_model_name(model: str) -> None:
    if not model.strip():
        raise ValueError("model registry name must not be empty")


@dataclass(frozen=True, slots=True)
class ModelPlannerConfig:
    """配置模型规划器。

    Args:
        model: 每次规划时从注册表解析的模型名称。
        default_executor: 步骤没有指定执行器时使用的名称。
        max_steps: 单个计划允许包含的最大步骤数。
        max_output_tokens: 规划响应的最大输出 Token 数。
        memory_namespace: 检索长期记忆时使用的命名空间。
        memory_limit: 最多注入提示词的记忆条数。
    """

    model: str
    default_executor: str = "default"
    max_steps: int = 20
    max_output_tokens: int = 4096
    memory_namespace: str = "default"
    memory_limit: int = 5

    def __post_init__(self) -> None:
        """校验可能导致失控规划或空注册表查询的配置。"""
        _validate_model_name(self.model)
        if not self.default_executor.strip():
            raise ValueError("default executor must not be empty")
        if self.max_steps < 1:
            raise ValueError("max steps must be at least 1")
        if self.max_output_tokens < 1:
            raise ValueError("max output tokens must be at least 1")
        if not self.memory_namespace.strip():
            raise ValueError("memory namespace must not be empty")
        if self.memory_limit < 1:
            raise ValueError("memory limit must be at least 1")


@dataclass(frozen=True, slots=True)
class ToolCallingWorkerConfig:
    """配置模型驱动的工具调用执行器。

    Args:
        model: 每轮从注册表解析的模型名称。
        tool_names: 明确授权给模型看到的工具名称；空元组表示不启用工具。
        max_tool_rounds: 一个步骤允许的最大工具反馈轮数。
        max_output_tokens: 单次模型响应的最大输出 Token 数。
    """

    model: str
    tool_names: tuple[str, ...] = ()
    max_tool_rounds: int = 8
    max_output_tokens: int = 4096

    def __post_init__(self) -> None:
        """保证工具循环存在硬边界且名称不含空值。"""
        _validate_model_name(self.model)
        if any(not name.strip() for name in self.tool_names):
            raise ValueError("tool names must not contain empty values")
        if len(set(self.tool_names)) != len(self.tool_names):
            raise ValueError("tool names must not contain duplicates")
        if self.max_tool_rounds < 1:
            raise ValueError("max tool rounds must be at least 1")
        if self.max_output_tokens < 1:
            raise ValueError("max output tokens must be at least 1")


@dataclass(frozen=True, slots=True)
class CriteriaVerifierConfig:
    """配置独立验收验证器。

    Args:
        model: 每次验证时从注册表解析的模型名称。
        pass_score: 即使模型声明通过，也必须达到的最低分数。
        max_output_tokens: 验证响应的最大输出 Token 数。
    """

    model: str
    pass_score: float = 80.0
    max_output_tokens: int = 2048

    def __post_init__(self) -> None:
        """校验分数范围和调用边界。"""
        _validate_model_name(self.model)
        if not 0 <= self.pass_score <= 100:
            raise ValueError("pass score must be between 0 and 100")
        if self.max_output_tokens < 1:
            raise ValueError("max output tokens must be at least 1")


@dataclass(frozen=True, slots=True)
class ModelReviewerConfig:
    """配置通用模型审查器。

    Args:
        model: 每次审查时从注册表解析的模型名称。
        max_output_tokens: 审查响应的最大输出 Token 数。
    """

    model: str
    max_output_tokens: int = 3072

    def __post_init__(self) -> None:
        """校验模型名称和输出预算。"""
        _validate_model_name(self.model)
        if self.max_output_tokens < 1:
            raise ValueError("max output tokens must be at least 1")
