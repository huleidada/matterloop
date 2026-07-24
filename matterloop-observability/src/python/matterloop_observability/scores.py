"""LLM 观测的一等评分数据模型与映射函数。"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from math import isfinite
from typing import Literal

from matterloop_core import VerificationResult

ScoreDataType = Literal["NUMERIC", "CATEGORICAL", "BOOLEAN"]
"""评分取值的数据类型。"""

ScoreSource = Literal["VERIFIER", "REVIEWER", "HUMAN", "EXTERNAL"]
"""评分的产生来源。"""


@dataclass(frozen=True, slots=True)
class Score:
    """附着在一次运行或步骤上的不可变评分记录。

    Args:
        name: 评分的稳定名称，例如 ``verification`` 或 ``review``。
        value: 评分取值，类型必须与 ``data_type`` 一致；NUMERIC 取值归一到 0 至 1。
        data_type: 评分取值的数据类型。
        source: 评分的产生来源。
        run_id: 评分所属的运行标识，即所属 trace 的标识。
        step_id: 评分针对的计划步骤标识；面向整个运行时保持为 ``None``。
        comment: 可选的评分说明。
        evidence: 支持评分判断的不可变证据条目。
        timestamp: 评分产生时间。
    """

    name: str
    value: float | str | bool
    data_type: ScoreDataType
    source: ScoreSource
    run_id: str
    step_id: str | None = None
    comment: str | None = None
    evidence: tuple[str, ...] = ()
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def __post_init__(self) -> None:
        """保证评分名称、标识和取值类型一致。"""
        if not self.name.strip():
            raise ValueError("score name must not be empty")
        if not self.run_id.strip():
            raise ValueError("score run_id must not be empty")
        if self.step_id is not None and not self.step_id.strip():
            raise ValueError("score step_id must not be empty")
        if self.data_type == "NUMERIC" and (
            isinstance(self.value, bool) or not isinstance(self.value, (int, float))
        ):
            raise TypeError("NUMERIC score value must be a number")
        if self.data_type == "NUMERIC" and (
            not isfinite(float(self.value)) or not 0.0 <= float(self.value) <= 1.0
        ):
            raise ValueError("NUMERIC score value must be finite and between 0 and 1")
        if self.data_type == "BOOLEAN" and not isinstance(self.value, bool):
            raise TypeError("BOOLEAN score value must be a bool")
        if self.data_type == "CATEGORICAL" and not isinstance(self.value, str):
            raise TypeError("CATEGORICAL score value must be text")
        if any(not item.strip() for item in self.evidence):
            raise ValueError("score evidence must not contain empty values")


def score_from_verification(
    run_id: str,
    step_id: str | None,
    result: VerificationResult,
) -> Score | None:
    """把验证结论映射为归一化的 NUMERIC 评分；没有评分时返回 ``None``。"""
    if result.score is None:
        return None
    return Score(
        name="verification",
        value=result.score / 100.0,
        data_type="NUMERIC",
        source="VERIFIER",
        run_id=run_id,
        step_id=step_id,
        comment=result.feedback or None,
        evidence=result.evidence,
    )


def score_from_review(
    run_id: str,
    review: object,
    step_id: str | None = None,
) -> Score | None:
    """把具备 ``score``/``summary``/``evidence`` 属性的审查结论映射为评分。

    为避免依赖 agents 组件，``review`` 采用鸭子类型；缺少数值评分时返回 ``None``。
    """
    raw_score = getattr(review, "score", None)
    if raw_score is None:
        return None
    summary = getattr(review, "summary", None)
    evidence = getattr(review, "evidence", ()) or ()
    return Score(
        name="review",
        value=float(raw_score) / 100.0,
        data_type="NUMERIC",
        source="REVIEWER",
        run_id=run_id,
        step_id=step_id,
        comment=str(summary) if summary else None,
        evidence=tuple(str(item) for item in evidence),
    )
