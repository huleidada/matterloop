"""实现可独立复用的模型审查器及 Verifier 适配器。"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass

from matterloop_core import ExecutionResult, LoopContext, PlanStep, VerificationResult
from matterloop_models import MessageRole, ModelMessage, ModelRegistry, ModelRequest

from matterloop_agents._parsing import (
    parse_json_object,
    require_score,
    require_string,
    string_tuple,
)
from matterloop_agents.config import ModelReviewerConfig

_REVIEW_SCHEMA: Mapping[str, object] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "score": {"type": "number", "minimum": 0, "maximum": 100},
        "summary": {"type": "string"},
        "evidence": {"type": "array", "items": {"type": "string"}},
        "issues": {"type": "array", "items": {"type": "string"}},
        "recommendations": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["score", "summary", "evidence", "issues", "recommendations"],
}


@dataclass(frozen=True, slots=True)
class ReviewResult:
    """保存通用审查结论，不直接绑定 Loop 的通过语义。

    Args:
        score: 零到一百之间的综合质量分数。
        summary: 面向调用方的审查摘要。
        evidence: 支持审查判断的证据。
        issues: 审查发现的具体问题。
        recommendations: 可执行的改进建议。
    """

    score: float
    summary: str
    evidence: tuple[str, ...] = ()
    issues: tuple[str, ...] = ()
    recommendations: tuple[str, ...] = ()


class ModelReviewer:
    """使用注册表模型对执行结果进行通用质量审查。

    Args:
        models: 支持热替换的模型注册表。
        config: 模型名称和输出预算配置。
    """

    def __init__(self, models: ModelRegistry, config: ModelReviewerConfig) -> None:
        self._models = models
        self._config = config

    async def review(
        self,
        step: PlanStep,
        result: ExecutionResult,
        context: LoopContext,
    ) -> ReviewResult:
        """依据步骤条件返回分数、问题和改进建议。

        Args:
            step: 需要审查的计划步骤。
            result: Worker 产生的执行结果。
            context: 当前 Loop 运行上下文。

        Returns:
            与 Loop 通过语义解耦的通用审查报告。
        """
        request = ModelRequest(
            messages=(
                ModelMessage(
                    MessageRole.DEVELOPER,
                    "你是独立工程审查员。评估正确性、完整性、证据质量和潜在风险，"
                    "只返回符合 Schema 的审查报告。",
                ),
                ModelMessage(
                    MessageRole.USER,
                    json.dumps(
                        {
                            "goal": context.request.goal,
                            "step": step.description,
                            "acceptance_criteria": list(step.acceptance_criteria),
                            "execution_output": result.output,
                        },
                        ensure_ascii=False,
                    ),
                ),
            ),
            response_schema=_REVIEW_SCHEMA,
            response_schema_name="matterloop_review",
            max_output_tokens=self._config.max_output_tokens,
            usage_scopes=self._usage_scopes(context),
            metadata={"run_id": context.run_id, "step_id": step.step_id, "agent": "reviewer"},
        )
        async with self._models.acquire(self._config.model) as model:
            response = await model.generate(request)
        value = parse_json_object(response.output_text, purpose="reviewer")
        return ReviewResult(
            score=require_score(value, "score", purpose="reviewer"),
            summary=require_string(value, "summary", purpose="reviewer"),
            evidence=string_tuple(value, "evidence", purpose="reviewer"),
            issues=string_tuple(value, "issues", purpose="reviewer"),
            recommendations=string_tuple(value, "recommendations", purpose="reviewer"),
        )

    @staticmethod
    def _usage_scopes(context: LoopContext) -> tuple[str, ...]:
        """读取由组合根显式注入的额度作用域。"""
        raw = context.request.metadata.get("usage_scopes", ())
        if not isinstance(raw, (tuple, list)):
            return ()
        return tuple(item for item in raw if isinstance(item, str) and item.strip())


class ReviewerVerifierAdapter:
    """把通用审查结果转换为内核 `Verifier` 所需的验收结论。

    Args:
        reviewer: 实际生成审查报告的模型审查器。
        pass_score: 转换为通过结果所需的最低分数。
    """

    def __init__(self, reviewer: ModelReviewer, *, pass_score: float = 80.0) -> None:
        if not 0 <= pass_score <= 100:
            raise ValueError("pass score must be between 0 and 100")
        self._reviewer = reviewer
        self._pass_score = pass_score

    async def verify(
        self,
        step: PlanStep,
        result: ExecutionResult,
        context: LoopContext,
    ) -> VerificationResult:
        """审查并采用保守规则生成验证结果。

        Args:
            step: 需要验证的计划步骤。
            result: Worker 产生的执行结果。
            context: 当前 Loop 运行上下文。

        Returns:
            达到阈值且没有审查问题时通过的内核验证结果。
        """
        review = await self._reviewer.review(step, result, context)
        return VerificationResult(
            passed=review.score >= self._pass_score and not review.issues,
            feedback=review.summary,
            score=review.score,
            evidence=review.evidence,
            failed_criteria=review.issues,
        )
