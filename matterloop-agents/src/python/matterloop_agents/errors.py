"""定义 Agent 输出与工具循环的类型化异常。"""


class AgentError(Exception):
    """所有标准 Agent 组件异常的基类。"""


class AgentModelOutputError(AgentError):
    """模型输出不符合 Agent 声明的结构或语义约束。"""


class PlanStepLimitError(AgentModelOutputError):
    """规划结果超过配置允许的最大步骤数。"""


class ToolLoopLimitError(AgentError):
    """模型持续请求工具并达到硬性轮数限制。"""


class UnauthorizedToolCallError(AgentError):
    """模型请求了未在执行器配置中授权的工具。"""


class ToolContinuationError(AgentError):
    """模型工具调用响应缺少继续会话所需的标识。"""
