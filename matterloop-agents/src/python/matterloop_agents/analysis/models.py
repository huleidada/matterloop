"""失败分析的类别、纠正策略与诊断值对象。"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from math import isfinite
from typing import Protocol

from matterloop_core import LoopResult


class FailureCategory(str, Enum):
    """Loop 失败的结构化归因类别。"""

    PLANNER_ERROR = "planner_error"
    TOOL_FAILURE = "tool_failure"
    PARAMETER_ERROR = "parameter_error"
    ENVIRONMENT_ERROR = "environment_error"
    VERIFICATION_FAILURE = "verification_failure"
    BUDGET_EXHAUSTED = "budget_exhausted"
    HUMAN_REJECTED = "human_rejected"
    TIMEOUT = "timeout"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class CorrectionStrategy:
    """针对一次失败给出的可执行纠正策略。

    Args:
        summary: 便于人类理解的策略摘要。
        replan_hints: 可直接并入下一轮 Loop 请求约束的重规划提示。
        suggested_actions: 结构化动作建议，例如 ``switch_tool:xxx`` 或 ``escalate_human``。
        confidence: 策略可信度，取值区间为 0 到 1。
    """

    summary: str
    replan_hints: tuple[str, ...] = ()
    suggested_actions: tuple[str, ...] = ()
    confidence: float = 0.5

    def __post_init__(self) -> None:
        """拒绝无法解释的置信度取值。"""
        if not isfinite(self.confidence) or not 0 <= self.confidence <= 1:
            raise ValueError("confidence must be between 0 and 1")


@dataclass(frozen=True, slots=True)
class FailureDiagnosis:
    """一次失败分析产出的完整诊断结论。

    Args:
        category: 失败归因类别。
        summary: 便于人类理解的失败摘要。
        evidence: 引用具体步骤或错误文本的证据条目。
        strategy: 建议采用的纠正策略。
    """

    category: FailureCategory
    summary: str
    evidence: tuple[str, ...]
    strategy: CorrectionStrategy


class FailureAnalyzer(Protocol):
    """把 Loop 终态结果映射为结构化诊断的分析器接口。"""

    async def analyze(self, result: LoopResult) -> FailureDiagnosis:
        """分析一次 Loop 结果并返回失败诊断。"""
        ...


__all__ = [
    "CorrectionStrategy",
    "FailureAnalyzer",
    "FailureCategory",
    "FailureDiagnosis",
]
