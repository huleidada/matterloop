"""MatterLoop 内核抛出的类型化异常。"""


class MatterLoopError(Exception):
    """内核可预期异常的统一基类。"""


class ComponentNotFoundError(MatterLoopError):
    """请求的组件尚未注册时抛出。"""


class ComponentAlreadyRegisteredError(MatterLoopError):
    """注册操作会意外覆盖已有组件时抛出。"""


class InvalidPluginError(MatterLoopError):
    """第三方 Entry Point 没有返回合法插件定义时抛出。"""


class InvalidPlanError(MatterLoopError):
    """规划器返回不可执行计划时抛出。"""


class CheckpointSchemaError(MatterLoopError):
    """检查点版本不受支持或数据结构损坏时抛出。"""


class CheckpointConflictError(MatterLoopError):
    """检查点 CAS revision 与当前存储版本不一致时抛出。"""


class InvalidStateTransitionError(MatterLoopError):
    """代码尝试执行非法生命周期转换时抛出。"""

    def __init__(self, current: str, target: str) -> None:
        super().__init__(f"invalid loop state transition: {current} -> {target}")


class LoopNotFoundError(MatterLoopError):
    """恢复操作找不到指定运行检查点时抛出。"""


class LoopNotResumableError(MatterLoopError):
    """指定运行当前状态不允许恢复时抛出。"""


class HumanInteractionNotPendingError(MatterLoopError):
    """提交的响应无法匹配当前待处理人工交互时抛出。"""


class HumanResponseConflictError(MatterLoopError):
    """同一幂等键被用于语义不同的人工响应时抛出。"""


class ResourceLimitExceededError(MatterLoopError):
    """本地计算、调用或费用额度不足以继续运行时抛出。"""
