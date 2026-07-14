"""基于规则的步骤审批策略。"""

from dataclasses import dataclass

from matterloop_core import ApprovalDecision, LoopContext, PlanStep


@dataclass(frozen=True, slots=True)
class ApprovalRule:
    """按 Executor 名称匹配审批结果。"""

    executor: str
    decision: ApprovalDecision


class RuleBasedApprovalGate:
    """按顺序匹配规则，并使用安全的默认审批结果。"""

    def __init__(
        self,
        rules: tuple[ApprovalRule, ...] = (),
        default: ApprovalDecision = ApprovalDecision.DEFERRED,
    ) -> None:
        self._rules = rules
        self._default = default

    async def decide(self, step: PlanStep, context: LoopContext) -> ApprovalDecision:
        """返回第一个匹配规则的审批结果。"""
        del context
        for rule in self._rules:
            if rule.executor == "*" or rule.executor == step.executor:
                return rule.decision
        return self._default


class AllowAllApproval:
    """为无需人工审批的 preset 批准全部受控步骤。"""

    async def decide(self, step: PlanStep, context: LoopContext) -> ApprovalDecision:
        """始终返回批准。"""
        del step, context
        return ApprovalDecision.APPROVED
