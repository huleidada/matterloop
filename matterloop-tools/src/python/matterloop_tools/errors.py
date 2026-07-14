"""MatterLoop 工具异常。"""


class ToolError(Exception):
    """所有工具组件异常的基类。"""


class ToolNotFoundError(ToolError):
    """请求的工具不存在。"""

    def __init__(self, name: str) -> None:
        """初始化异常。

        Args:
            name: 未找到的工具名称。
        """
        super().__init__(f"tool not found: {name}")


class ToolPermissionDeniedError(ToolError):
    """权限策略拒绝了工具调用。"""

    def __init__(self, name: str) -> None:
        """初始化异常。

        Args:
            name: 被拒绝的工具名称。
        """
        super().__init__(f"tool call denied: {name}")


class ToolInputError(ToolError):
    """工具参数缺失、类型错误或越过安全边界。"""


class ToolConfigurationError(ToolError):
    """工具构造配置不完整或不安全。"""
