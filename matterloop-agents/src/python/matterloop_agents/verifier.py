"""实现按明确验收条件独立判定执行结果的模型验证器。"""

from __future__ import annotations

import json
from collections.abc import Mapping

from matterloop_core import ExecutionResult, LoopContext, PlanStep, VerificationResult
from matterloop_models import MessageRole, ModelMessage, ModelRegistry, ModelRequest

from matterloop_agents._parsing import (
    parse_json_object,
    require_boolean,
    require_score,
    require_string,
    string_tuple,
)
from matterloop_agents.config import CriteriaVerifierConfig

_VERIFICATION_SCHEMA: Mapping[str, object] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "passed": {"type": "boolean"},
        "score": {"type": "number", "minimum": 0, "maximum": 100},
        "feedback": {"type": "string"},
        "evidence": {"type": "array", "items": {"type": "string"}},
        "failed_criteria": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["passed", "score", "feedback", "evidence", "failed_criteria"],
}


class CriteriaVerifier:
    """使用独立模型逐条检查步骤验收条件。

    Args:
        models: 支持热替换的模型注册表。
        config: 验证分数阈值和输出预算配置。
    """

    def __init__(self, models: ModelRegistry, config: CriteriaVerifierConfig) -> None:
        self._models = models
        self._config = config

    async def verify(
        self,
        step: PlanStep,
        result: ExecutionResult,
        context: LoopContext,
    ) -> VerificationResult:
        """返回带分数、证据和失败条件的保守验证结论。

        Args:
            step: 正在验收的计划步骤。
            result: Worker 产生的输出和制品引用。
            context: 当前 Loop 运行上下文。

        Returns:
            只有模型声明通过、达到阈值且没有失败条件时才通过的结果。
        """
        request = ModelRequest(
            messages=(
                ModelMessage(
                    MessageRole.DEVELOPER,
                    "你是独立验证器。只依据提供的结果、产物引用和验收条件判断；"
                    "没有证据时不得推定通过。",
                ),
                ModelMessage(MessageRole.USER, self._verification_payload(step, result, context)),
            ),
            response_schema=_VERIFICATION_SCHEMA,
            response_schema_name="matterloop_verification",
            max_output_tokens=self._config.max_output_tokens,
            usage_scopes=self._usage_scopes(context),
            metadata={"run_id": context.run_id, "step_id": step.step_id, "agent": "verifier"},
        )
        async with self._models.acquire(self._config.model) as model:
            response = await model.generate(request)
        value = parse_json_object(response.output_text, purpose="verifier")
        score = require_score(value, "score", purpose="verifier")
        failed_criteria = string_tuple(value, "failed_criteria", purpose="verifier")
        model_passed = require_boolean(value, "passed", purpose="verifier")
        passed = model_passed and score >= self._config.pass_score and not failed_criteria
        return VerificationResult(
            passed=passed,
            feedback=require_string(value, "feedback", purpose="verifier"),
            score=score,
            evidence=string_tuple(value, "evidence", purpose="verifier"),
            failed_criteria=failed_criteria,
        )

    @staticmethod
    def _usage_scopes(context: LoopContext) -> tuple[str, ...]:
        """读取由组合根显式注入的额度作用域。"""
        raw = context.request.metadata.get("usage_scopes", ())
        if not isinstance(raw, (tuple, list)):
            return ()
        return tuple(item for item in raw if isinstance(item, str) and item.strip())

    @staticmethod
    def _verification_payload(
        step: PlanStep,
        result: ExecutionResult,
        context: LoopContext,
    ) -> str:
        artifacts = [
            {
                "name": artifact.name,
                "uri": artifact.uri,
                "media_type": artifact.media_type,
            }
            for artifact in result.artifacts
        ]
        criteria = step.acceptance_criteria or context.request.acceptance_criteria
        return json.dumps(
            {
                "goal": context.request.goal,
                "step": step.description,
                "acceptance_criteria": list(criteria),
                "execution_output": result.output,
                "artifacts": artifacts,
            },
            ensure_ascii=False,
        )
