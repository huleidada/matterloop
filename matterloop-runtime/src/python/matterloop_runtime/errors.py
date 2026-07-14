"""MatterLoop 运行时异常。"""


class RuntimeErrorBase(Exception):
    """所有运行时组件异常的基类。"""


class RuntimeClosedError(RuntimeErrorBase):
    """运行时或容器已经关闭。"""


class ComponentExistsError(RuntimeErrorBase):
    """注册同名组件但未允许替换。"""

    def __init__(self, name: str) -> None:
        """初始化异常。

        Args:
            name: 已存在的组件名称。
        """
        super().__init__(f"component already exists: {name}")


class ComponentNotFoundError(RuntimeErrorBase):
    """请求的运行时组件不存在。"""

    def __init__(self, name: str) -> None:
        """初始化异常。

        Args:
            name: 未找到的组件名称。
        """
        super().__init__(f"component not found: {name}")


class DuplicateRunError(RuntimeErrorBase):
    """队列或运行仓储中已存在同一运行标识。"""

    def __init__(self, run_id: str) -> None:
        """初始化异常。

        Args:
            run_id: 重复的运行标识。
        """
        super().__init__(f"run already exists: {run_id}")


class RunNotFoundError(RuntimeErrorBase):
    """指定运行记录不存在。"""

    def __init__(self, run_id: str) -> None:
        """初始化异常。

        Args:
            run_id: 未找到的运行标识。
        """
        super().__init__(f"run not found: {run_id}")


class RunNotResumableError(RuntimeErrorBase):
    """指定运行当前不允许恢复。"""


class SandboxError(RuntimeErrorBase):
    """沙箱请求无法安全执行。"""


class SandboxPathError(SandboxError):
    """请求的工作目录逃逸出允许根目录。"""
