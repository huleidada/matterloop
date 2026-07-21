"""审批、重试与整体完成验收使用的标准化决策值对象。"""

from dataclasses import dataclass
from enum import Enum
from math import isfinite

from matterloop_core.context.human import HumanInteractionRequest


class ApprovalDecision(str, Enum):
    """审批组件可以返回的标准化决策。"""

    APPROVED = "approved"
    REJECTED = "rejected"
    DEFERRED = "deferred"


class RetryAction(str, Enum):
    """组件执行异常后的标准化处理动作。"""

    RETRY = "retry"
    REPLAN = "replan"
    FAIL = "fail"


@dataclass(frozen=True, slots=True)
class RetryDecision:
    """描述异常发生后的处理动作和可选等待时间。"""

    action: RetryAction
    delay_seconds: float = 0

    def __post_init__(self) -> None:
        """禁止负数等待时间进入异步调度器。"""
        if not isfinite(self.delay_seconds) or self.delay_seconds < 0:
            raise ValueError("delay_seconds must be finite and not negative")


class CompletionAction(str, Enum):
    """整体目标验收后可以采取的标准动作。"""

    ACCEPT = "accept"
    REPLAN = "replan"
    REQUEST_HUMAN = "request_human"
    STOP = "stop"


@dataclass(frozen=True, slots=True)
class CompletionDecision:
    """描述全部计划步骤完成后的整体目标验收结论。

    Args:
        action: 接受、重新规划、请求人类或停止运行。
        feedback: 传递给后续规划器或公开结果的验收意见。
        interaction: 请求人类时必须提供的结构化交互。
    """

    action: CompletionAction
    feedback: str = ""
    interaction: HumanInteractionRequest | None = None

    def __post_init__(self) -> None:
        """保证人工请求与决策动作一致。"""
        if self.action is CompletionAction.REQUEST_HUMAN and self.interaction is None:
            raise ValueError("request_human decision requires an interaction")
        if self.action is not CompletionAction.REQUEST_HUMAN and self.interaction is not None:
            raise ValueError("interaction is only valid for request_human decision")
