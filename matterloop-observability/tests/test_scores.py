"""评分模型与映射函数测试。"""

from dataclasses import dataclass
from math import nan

import pytest
from matterloop_core import VerificationResult
from matterloop_observability import Score, score_from_review, score_from_verification


def test_score_from_verification_normalizes_numeric_value() -> None:
    """验证评分应归一到 0 至 1 并保留反馈与证据。"""
    result = VerificationResult(passed=True, feedback="符合验收", score=80.0, evidence=("日志",))

    score = score_from_verification("run-1", "step-1", result)

    assert score is not None
    assert score.name == "verification"
    assert score.value == 0.8
    assert score.data_type == "NUMERIC"
    assert score.source == "VERIFIER"
    assert score.run_id == "run-1"
    assert score.step_id == "step-1"
    assert score.comment == "符合验收"
    assert score.evidence == ("日志",)


@pytest.mark.parametrize("raw", [0.0, 100.0])
def test_score_from_verification_keeps_boundary_values(raw: float) -> None:
    """区间边界的评分应精确归一。"""
    result = VerificationResult(passed=raw > 0, score=raw)

    score = score_from_verification("run-1", None, result)

    assert score is not None
    assert score.value == raw / 100.0
    assert score.step_id is None
    assert score.comment is None


def test_score_from_verification_without_score_returns_none() -> None:
    """没有数值评分的验证结论不应产生 Score。"""
    result = VerificationResult(passed=False, feedback="缺少评分")

    assert score_from_verification("run-1", "step-1", result) is None


@dataclass(frozen=True)
class _Review:
    """模拟 agents 组件审查结论的鸭子类型。"""

    score: float | None
    summary: str
    evidence: tuple[str, ...] = ()


def test_score_from_review_maps_duck_typed_result() -> None:
    """审查结论应映射为 REVIEWER 来源的归一评分。"""
    score = score_from_review("run-1", _Review(score=60.0, summary="基本达标", evidence=("片段",)))

    assert score is not None
    assert score.name == "review"
    assert score.value == 0.6
    assert score.source == "REVIEWER"
    assert score.comment == "基本达标"
    assert score.evidence == ("片段",)


def test_score_from_review_without_score_returns_none() -> None:
    """缺少数值评分的审查结论不应产生 Score。"""
    assert score_from_review("run-1", _Review(score=None, summary="无法评分")) is None


def test_score_rejects_value_type_mismatch() -> None:
    """取值类型与声明的数据类型不一致时必须拒绝。"""
    with pytest.raises(TypeError, match="NUMERIC"):
        Score(name="quality", value="high", data_type="NUMERIC", source="HUMAN", run_id="r")
    with pytest.raises(TypeError, match="BOOLEAN"):
        Score(name="ok", value=1, data_type="BOOLEAN", source="HUMAN", run_id="r")


@pytest.mark.parametrize("value", [-0.1, 1.1, nan])
def test_score_rejects_out_of_range_numeric_value(value: float) -> None:
    """NUMERIC 分数只能使用有限且已归一化的值。"""
    with pytest.raises(ValueError, match="between 0 and 1"):
        Score(name="quality", value=value, data_type="NUMERIC", source="HUMAN", run_id="r")


def test_score_rejects_empty_identity() -> None:
    """评分名称和运行标识不能为空。"""
    with pytest.raises(ValueError, match="name"):
        Score(name=" ", value=1.0, data_type="NUMERIC", source="HUMAN", run_id="r")
    with pytest.raises(ValueError, match="run_id"):
        Score(name="quality", value=1.0, data_type="NUMERIC", source="HUMAN", run_id=" ")
