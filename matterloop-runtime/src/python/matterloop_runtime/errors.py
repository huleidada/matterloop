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


class RunRequestConflictError(DuplicateRunError):
    """同一运行标识已绑定到不同请求。"""

    def __init__(self, run_id: str) -> None:
        """初始化异常。

        Args:
            run_id: 已被其他请求占用的运行标识。
        """
        RuntimeErrorBase.__init__(self, f"run id is already bound to another request: {run_id}")


class RunUpdateConflictError(RuntimeErrorBase):
    """运行记录在限定次数内持续发生 CAS 冲突。"""

    def __init__(self, run_id: str, attempts: int) -> None:
        """初始化异常。

        Args:
            run_id: 更新失败的运行标识。
            attempts: 已执行的 CAS 尝试次数。
        """
        super().__init__(f"run update conflict after {attempts} attempts: {run_id}")


class QueueLeaseLostError(RuntimeErrorBase):
    """队列租约已过期、已续期或不再由当前工作进程持有。"""

    def __init__(self, lease_id: str) -> None:
        """初始化异常。

        Args:
            lease_id: 已失效的租约标识。
        """
        super().__init__(f"queue lease is no longer current: {lease_id}")


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
