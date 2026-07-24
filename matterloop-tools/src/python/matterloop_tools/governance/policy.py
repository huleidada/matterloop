"""MCP 工具风险分级策略。"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from enum import Enum

_MIN_RISK_SCORE = 0
_MAX_RISK_SCORE = 100
_DEFAULT_UNREGISTERED_NOTES = "未注册工具回退到策略集默认访问级别"


class ToolAccessLevel(str, Enum):
    """治理策略允许的工具访问级别。"""

    READ_ONLY = "read_only"
    WRITE = "write"
    APPROVAL_REQUIRED = "approval_required"


@dataclass(frozen=True, slots=True)
class ToolPolicy:
    """单个工具的治理策略与风险分级。

    Args:
        tool_name: 注册表中的工具名称。
        access_level: 工具被允许的访问级别。
        risk_score: 0 到 100 的风险评分，越高越危险。
        max_calls_per_run: 单次 Loop 运行内允许的最大调用次数；``None`` 表示不限制。
        notes: 供审计与运营参考的策略备注。
    """

    tool_name: str
    access_level: ToolAccessLevel
    risk_score: int
    max_calls_per_run: int | None = None
    notes: str = ""

    def __post_init__(self) -> None:
        """校验策略字段并拒绝不安全取值。"""
        if not self.tool_name.strip():
            raise ValueError("tool_name must not be empty")
        try:
            access_level = ToolAccessLevel(self.access_level)
        except ValueError as exc:
            raise ValueError("access_level must be a valid ToolAccessLevel") from exc
        if type(self.risk_score) is not int:
            raise ValueError("risk_score must be an integer")
        if not _MIN_RISK_SCORE <= self.risk_score <= _MAX_RISK_SCORE:
            raise ValueError("risk_score must be between 0 and 100")
        if self.max_calls_per_run is not None and (
            type(self.max_calls_per_run) is not int or self.max_calls_per_run <= 0
        ):
            raise ValueError("max_calls_per_run must be a positive integer when provided")
        object.__setattr__(self, "access_level", access_level)


class ToolPolicySet:
    """维护工具名称到治理策略的映射。

    未注册工具按可配置的默认访问级别处理；出于安全默认，缺省为
    ``APPROVAL_REQUIRED`` 且风险评分取最保守的 100。

    Args:
        policies: 初始策略集合，工具名称必须唯一。
        default_policy: 未注册工具回退使用的访问级别。
    """

    def __init__(
        self,
        policies: Iterable[ToolPolicy] = (),
        *,
        default_policy: ToolAccessLevel = ToolAccessLevel.APPROVAL_REQUIRED,
    ) -> None:
        try:
            self._default_policy = ToolAccessLevel(default_policy)
        except ValueError as exc:
            raise ValueError("default_policy must be a valid ToolAccessLevel") from exc
        self._policies: dict[str, ToolPolicy] = {}
        for policy in policies:
            self.register(policy)

    @property
    def default_policy(self) -> ToolAccessLevel:
        """返回未注册工具使用的默认访问级别。"""
        return self._default_policy

    def register(self, policy: ToolPolicy, *, replace: bool = False) -> None:
        """注册一个工具策略。

        Args:
            policy: 需要注册的策略。
            replace: 同名策略存在时是否替换。

        Raises:
            ValueError: 策略已存在且不允许替换。
        """
        if not replace and policy.tool_name in self._policies:
            raise ValueError(f"tool policy already registered: {policy.tool_name}")
        self._policies[policy.tool_name] = policy

    def get(self, tool_name: str) -> ToolPolicy | None:
        """返回已注册的策略；不存在时返回 ``None``。"""
        return self._policies.get(tool_name)

    def names(self) -> tuple[str, ...]:
        """返回稳定排序的已注册工具名称。"""
        return tuple(sorted(self._policies))

    def classify(self, tool_name: str) -> ToolPolicy:
        """对工具做风险分级。

        Args:
            tool_name: 需要分级的工具名称。

        Returns:
            已注册策略；未注册时返回按默认访问级别合成的保守策略。
        """
        registered = self._policies.get(tool_name)
        if registered is not None:
            return registered
        return ToolPolicy(
            tool_name,
            self._default_policy,
            risk_score=_MAX_RISK_SCORE,
            notes=_DEFAULT_UNREGISTERED_NOTES,
        )
