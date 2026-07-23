"""基于确定性规则的失败分析器实现。"""

from __future__ import annotations

from collections.abc import Mapping

from matterloop_core import IterationRecord, LoopResult
from matterloop_core.state import StopReason

from matterloop_agents.analysis.models import (
    CorrectionStrategy,
    FailureCategory,
    FailureDiagnosis,
)

_DEFAULT_PATTERNS: tuple[tuple[str, FailureCategory], ...] = (
    ("permission denied", FailureCategory.ENVIRONMENT_ERROR),
    ("access denied", FailureCategory.ENVIRONMENT_ERROR),
    ("connection refused", FailureCategory.ENVIRONMENT_ERROR),
    ("not found", FailureCategory.PARAMETER_ERROR),
    ("timed out", FailureCategory.TIMEOUT),
    ("timeout", FailureCategory.TIMEOUT),
    ("invalid argument", FailureCategory.TOOL_FAILURE),
    ("tool error", FailureCategory.TOOL_FAILURE),
)

_BUDGET_STOP_REASONS = frozenset(
    {StopReason.BUDGET_EXHAUSTED, StopReason.CYCLE_LIMIT, StopReason.ATTEMPT_LIMIT}
)
_HUMAN_STOP_REASONS = frozenset(
    {
        StopReason.HUMAN_REJECTED,
        StopReason.APPROVAL_REJECTED,
        StopReason.COMPLETION_REJECTED,
    }
)


class RuleBasedFailureAnalyzer:
    """按固定优先级规则归因失败并生成纠正策略。

    规则顺序为：预算类停止原因、人工拒绝、超时、步骤超限、验证失败聚集、
    错误文本模式匹配、空计划兜底，最后回落为未知类别。

    Args:
        extra_patterns: 追加的错误文本模式表，匹配优先级高于内置模式。
        verification_failure_threshold: 判定为验证失败类别所需的最少失败验证次数。
    """

    def __init__(
        self,
        *,
        extra_patterns: Mapping[str, FailureCategory] | None = None,
        verification_failure_threshold: int = 2,
    ) -> None:
        if verification_failure_threshold < 1:
            raise ValueError("verification_failure_threshold must be at least 1")
        injected = tuple((key.lower(), value) for key, value in (extra_patterns or {}).items())
        self._patterns: tuple[tuple[str, FailureCategory], ...] = injected + _DEFAULT_PATTERNS
        self._verification_failure_threshold = verification_failure_threshold

    async def analyze(self, result: LoopResult) -> FailureDiagnosis:
        """分析一次 Loop 结果并返回失败诊断。

        Args:
            result: Loop 运行的不可变终态结果。

        Returns:
            包含类别、证据与纠正策略的诊断结论。
        """
        # 第一优先级：结构化停止原因能够直接给出确定性归因。
        if result.stop_reason in _BUDGET_STOP_REASONS:
            return self._budget_diagnosis(result)
        if result.stop_reason in _HUMAN_STOP_REASONS:
            return self._human_diagnosis(result)
        if result.stop_reason is StopReason.TIMED_OUT:
            return self._timeout_diagnosis(
                result, evidence=(f"stop_reason={result.stop_reason.value}",)
            )
        if result.stop_reason is StopReason.STEP_LIMIT:
            return self._planner_diagnosis(
                result,
                reason="计划步骤数量超过上限",
                evidence=(f"stop_reason={result.stop_reason.value}",),
            )

        # 第二优先级：多次验证失败说明产出质量问题而非单点执行故障。
        failed_records = [record for record in result.records if not record.verification.passed]
        if len(failed_records) >= self._verification_failure_threshold:
            return self._verification_diagnosis(failed_records)

        # 第三优先级：按模式表匹配错误文本，注入模式优先于内置模式。
        matched = self._match_patterns(result, failed_records)
        if matched is not None:
            return matched

        # 兜底规则：没有任何迭代记录说明规划阶段未产出可执行计划。
        if not result.records:
            return self._planner_diagnosis(
                result, reason="计划为空，没有产生任何迭代记录", evidence=("records is empty",)
            )
        return self._unknown_diagnosis(result)

    def _match_patterns(
        self, result: LoopResult, failed_records: list[IterationRecord]
    ) -> FailureDiagnosis | None:
        """在错误文本中匹配模式表并返回对应诊断。"""
        texts = [text for text in self._error_texts(result, failed_records) if text.strip()]
        for pattern, category in self._patterns:
            for text in texts:
                if pattern in text.lower():
                    evidence = (f"matched pattern {pattern!r} in: {text}",)
                    return self._pattern_diagnosis(category, pattern, evidence)
        return None

    @staticmethod
    def _error_texts(result: LoopResult, failed_records: list[IterationRecord]) -> list[str]:
        """收集用于模式匹配的错误相关文本。"""
        texts = [result.error]
        for record in failed_records:
            texts.append(record.execution.output)
            texts.append(record.verification.feedback)
        return texts

    @staticmethod
    def _budget_diagnosis(result: LoopResult) -> FailureDiagnosis:
        """构造预算耗尽类别的诊断。"""
        reason = result.stop_reason.value if result.stop_reason is not None else "unknown"
        return FailureDiagnosis(
            category=FailureCategory.BUDGET_EXHAUSTED,
            summary=f"执行预算耗尽后停止（stop_reason={reason}）",
            evidence=(
                f"stop_reason={reason}",
                f"cycles={result.cycles}",
                f"total_attempts={result.total_attempts}",
            ),
            strategy=CorrectionStrategy(
                summary="提高循环预算或简化计划后重试",
                replan_hints=("将目标拆分为更小的可验证步骤", "去掉与验收条件无关的步骤"),
                suggested_actions=("increase_budget", "simplify_plan"),
                confidence=0.9,
            ),
        )

    @staticmethod
    def _human_diagnosis(result: LoopResult) -> FailureDiagnosis:
        """构造人工拒绝类别的诊断。"""
        feedback = tuple(
            record.response.content
            for record in result.human_interactions
            if record.response.content.strip()
        )
        return FailureDiagnosis(
            category=FailureCategory.HUMAN_REJECTED,
            summary="人工拒绝了计划或结果",
            evidence=feedback or ("human rejected the loop",),
            strategy=CorrectionStrategy(
                summary="按照人工反馈意见调整目标或计划后重新规划",
                replan_hints=feedback or ("在重新规划前征求人工的具体修改意见",),
                suggested_actions=("replan_with_human_feedback", "escalate_human"),
                confidence=0.9,
            ),
        )

    @staticmethod
    def _timeout_diagnosis(result: LoopResult, *, evidence: tuple[str, ...]) -> FailureDiagnosis:
        """构造超时类别的诊断。"""
        return FailureDiagnosis(
            category=FailureCategory.TIMEOUT,
            summary="运行超时后停止",
            evidence=evidence,
            strategy=CorrectionStrategy(
                summary="提高超时上限或缩短单步执行时间",
                replan_hints=("优先执行耗时最短且能验证进展的步骤",),
                suggested_actions=("increase_timeout", "simplify_plan"),
                confidence=0.8,
            ),
        )

    @staticmethod
    def _planner_diagnosis(
        result: LoopResult, *, reason: str, evidence: tuple[str, ...]
    ) -> FailureDiagnosis:
        """构造规划错误类别的诊断。"""
        return FailureDiagnosis(
            category=FailureCategory.PLANNER_ERROR,
            summary=reason,
            evidence=evidence,
            strategy=CorrectionStrategy(
                summary="重新规划并控制计划规模",
                replan_hints=("生成非空且步骤数量在预算内的计划",),
                suggested_actions=("replan",),
                confidence=0.7,
            ),
        )

    @staticmethod
    def _verification_diagnosis(failed_records: list[IterationRecord]) -> FailureDiagnosis:
        """构造验证失败类别的诊断，并把验证反馈收进证据与提示。"""
        evidence = tuple(
            f"step {record.step_index} ({record.step.description}): {record.verification.feedback}"
            for record in failed_records
        )
        feedback_hints = tuple(
            dict.fromkeys(
                record.verification.feedback
                for record in failed_records
                if record.verification.feedback.strip()
            )
        )
        return FailureDiagnosis(
            category=FailureCategory.VERIFICATION_FAILURE,
            summary=f"共有 {len(failed_records)} 次验证未通过",
            evidence=evidence,
            strategy=CorrectionStrategy(
                summary="针对验证反馈修正实现后重新规划",
                replan_hints=feedback_hints + ("逐条满足此前未通过的验收条件",),
                suggested_actions=("replan",),
                confidence=0.8,
            ),
        )

    @staticmethod
    def _pattern_diagnosis(
        category: FailureCategory, pattern: str, evidence: tuple[str, ...]
    ) -> FailureDiagnosis:
        """根据命中的错误文本模式构造诊断。"""
        strategies: dict[FailureCategory, CorrectionStrategy] = {
            FailureCategory.ENVIRONMENT_ERROR: CorrectionStrategy(
                summary="运行环境权限或连通性异常，需要先修复环境",
                replan_hints=("在计划首步校验运行环境与访问权限",),
                suggested_actions=("check_environment", "escalate_human"),
                confidence=0.7,
            ),
            FailureCategory.PARAMETER_ERROR: CorrectionStrategy(
                summary="目标资源或参数不存在，需要修正输入参数",
                replan_hints=("在调用工具前先确认目标资源存在且参数拼写正确",),
                suggested_actions=("fix_parameters",),
                confidence=0.7,
            ),
            FailureCategory.TIMEOUT: CorrectionStrategy(
                summary="执行过程超时，需要缩短步骤或提高时限",
                replan_hints=("把耗时操作拆分为可分段验证的小步骤",),
                suggested_actions=("increase_timeout", "simplify_plan"),
                confidence=0.7,
            ),
            FailureCategory.TOOL_FAILURE: CorrectionStrategy(
                summary="工具调用失败，考虑更换工具或修正调用方式",
                replan_hints=("为失败步骤选择替代工具或调整调用参数",),
                suggested_actions=("switch_tool", "fix_parameters"),
                confidence=0.7,
            ),
        }
        strategy = strategies.get(
            category,
            CorrectionStrategy(
                summary="根据命中的错误模式调整计划后重试",
                replan_hints=(f"规避触发 {pattern!r} 的操作",),
                suggested_actions=("replan",),
                confidence=0.6,
            ),
        )
        return FailureDiagnosis(
            category=category,
            summary=f"错误文本命中模式 {pattern!r}",
            evidence=evidence,
            strategy=strategy,
        )

    @staticmethod
    def _unknown_diagnosis(result: LoopResult) -> FailureDiagnosis:
        """构造无法归因时的兜底诊断。"""
        reason = result.stop_reason.value if result.stop_reason is not None else "none"
        evidence = tuple(
            item
            for item in (f"status={result.status.value}", f"stop_reason={reason}", result.error)
            if item
        )
        return FailureDiagnosis(
            category=FailureCategory.UNKNOWN,
            summary="现有规则无法归因本次失败",
            evidence=evidence,
            strategy=CorrectionStrategy(
                summary="建议升级人工分析后再决定重试方式",
                replan_hints=(),
                suggested_actions=("escalate_human",),
                confidence=0.2,
            ),
        )


__all__ = ["RuleBasedFailureAnalyzer"]
