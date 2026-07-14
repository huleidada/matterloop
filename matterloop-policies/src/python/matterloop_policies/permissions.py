"""工具调用权限规则。"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from matterloop_tools import PermissionDecision, ToolContext


@dataclass(frozen=True, slots=True)
class PermissionRule:
    """定义工具、操作和权限结果的匹配规则。"""

    tool: str
    operations: tuple[str, ...]
    decision: PermissionDecision


class RuleBasedPermissionPolicy:
    """默认拒绝未命中规则的工具调用。"""

    def __init__(
        self,
        rules: tuple[PermissionRule, ...] = (),
        default: PermissionDecision = PermissionDecision.DENY,
    ) -> None:
        self._rules = rules
        self._default = default

    async def authorize(
        self,
        tool_name: str,
        arguments: Mapping[str, object],
        context: ToolContext,
    ) -> PermissionDecision:
        """根据工具名称和 operation 参数返回权限决策。"""
        del context
        operation_value = arguments.get("operation", "invoke")
        operation = operation_value if isinstance(operation_value, str) else "invoke"
        for rule in self._rules:
            tool_matches = rule.tool in {"*", tool_name}
            operation_matches = "*" in rule.operations or operation in rule.operations
            if tool_matches and operation_matches:
                return rule.decision
        return self._default
