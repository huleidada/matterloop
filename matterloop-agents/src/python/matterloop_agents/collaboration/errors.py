"""多智能体协作层的类型化异常。"""


class CollaborationError(Exception):
    """所有团队协作异常的基类。"""


class InvalidTaskGraphError(CollaborationError):
    """任务图包含重复标识、缺失依赖或依赖环。"""


class InvalidTaskTransitionError(CollaborationError):
    """任务状态转换不符合协作状态机约束。"""


class AgentAlreadyRegisteredError(CollaborationError):
    """同名 Agent 已存在且调用方没有允许替换。"""


class AgentNotFoundError(CollaborationError):
    """Agent 目录中不存在指定标识。"""


class NoCapableAgentError(CollaborationError):
    """没有 Agent 能够处理任务要求的能力。"""


class AgentCapacityError(CollaborationError):
    """所有匹配 Agent 当前都已达到并发上限。"""


class TeamRunAlreadyExistsError(CollaborationError):
    """团队运行标识已经存在。"""


class TeamRunNotFoundError(CollaborationError):
    """找不到指定团队运行状态。"""


class TeamRunActiveError(CollaborationError):
    """团队运行已经被另一个控制器实例持有执行租约。"""


class TeamStateConflictError(CollaborationError):
    """团队状态版本不匹配，CAS 更新被拒绝。"""


class TeamExecutionError(CollaborationError):
    """团队控制器无法继续执行当前任务图。"""


class TeamRuntimeClosedError(CollaborationError):
    """团队运行门面已经关闭，不能接受新调用。"""


class ArtifactNotFoundError(CollaborationError):
    """共享制品存储中不存在指定引用。"""


__all__ = [
    "AgentAlreadyRegisteredError",
    "AgentCapacityError",
    "AgentNotFoundError",
    "ArtifactNotFoundError",
    "CollaborationError",
    "InvalidTaskGraphError",
    "InvalidTaskTransitionError",
    "NoCapableAgentError",
    "TeamExecutionError",
    "TeamRunAlreadyExistsError",
    "TeamRunActiveError",
    "TeamRunNotFoundError",
    "TeamRuntimeClosedError",
    "TeamStateConflictError",
]
