"""基于主体身份的 MCP 工具权限控制。"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from fnmatch import fnmatchcase
from typing import Protocol, runtime_checkable

from matterloop_tools.errors import ToolError


class ToolAccessDeniedError(ToolError):
    """治理层拒绝了工具调用。"""

    def __init__(self, tool_name: str, reason: str) -> None:
        """初始化异常。

        Args:
            tool_name: 被拒绝的工具名称。
            reason: 拒绝原因，用于审计与排障。
        """
        super().__init__(f"tool access denied: {tool_name}: {reason}")
        self.tool_name = tool_name
        self.reason = reason


@dataclass(frozen=True, slots=True)
class Principal:
    """发起工具调用的主体身份。

    Args:
        agent_id: Agent 标识，必填。
        user_id: 终端用户标识；系统级调用可为空。
        tenant_id: 租户标识；单租户部署可为空。
        roles: 主体拥有的角色集合。
    """

    agent_id: str
    user_id: str | None = None
    tenant_id: str | None = None
    roles: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        """校验主体标识并冻结角色元组。"""
        if not self.agent_id.strip():
            raise ValueError("agent_id must not be empty")
        roles = tuple(self.roles)
        if any(not isinstance(role, str) or not role.strip() for role in roles):
            raise ValueError("roles must be non-empty strings")
        object.__setattr__(self, "roles", roles)


@dataclass(frozen=True, slots=True)
class AccessDecision:
    """一次访问控制判断的结果。

    Args:
        allowed: 是否允许调用。
        reason: 决策原因，写入审计记录。
    """

    allowed: bool
    reason: str


@runtime_checkable
class AccessController(Protocol):
    """工具调用前执行的访问控制协议。"""

    async def authorize(
        self,
        principal: Principal,
        tool_name: str,
        arguments: Mapping[str, object],
    ) -> AccessDecision:
        """判断主体是否可以以给定参数调用工具。"""
        ...


@dataclass(frozen=True, slots=True)
class AccessRule:
    """一条允许型访问规则。

    所有非空匹配条件必须同时命中；``allowed_tools`` 中任意一个 ``fnmatch``
    模式匹配工具名称即视为允许。规则只表达允许，未被任何规则命中的调用
    由控制器默认拒绝。

    Args:
        allowed_tools: 工具名称模式元组，支持 ``fnmatch`` 通配。
        role: 要求主体拥有的角色；``None`` 表示不限。
        agent_id: 要求的 Agent 标识；``None`` 表示不限。
        user_id: 要求的用户标识；``None`` 表示不限。
        tenant_id: 要求的租户标识；``None`` 表示不限。
    """

    allowed_tools: tuple[str, ...]
    role: str | None = None
    agent_id: str | None = None
    user_id: str | None = None
    tenant_id: str | None = None

    def __post_init__(self) -> None:
        """校验并冻结工具模式元组。"""
        patterns = tuple(self.allowed_tools)
        if not patterns:
            raise ValueError("allowed_tools must not be empty")
        if any(not isinstance(pattern, str) or not pattern.strip() for pattern in patterns):
            raise ValueError("allowed_tools patterns must be non-empty strings")
        object.__setattr__(self, "allowed_tools", patterns)

    def matches(self, principal: Principal, tool_name: str) -> bool:
        """判断规则是否允许该主体调用该工具。

        Args:
            principal: 发起调用的主体。
            tool_name: 工具注册名称。

        Returns:
            所有身份条件命中且工具名称匹配任一模式时返回 ``True``。
        """
        if self.role is not None and self.role not in principal.roles:
            return False
        if self.agent_id is not None and self.agent_id != principal.agent_id:
            return False
        if self.user_id is not None and self.user_id != principal.user_id:
            return False
        if self.tenant_id is not None and self.tenant_id != principal.tenant_id:
            return False
        return any(fnmatchcase(tool_name, pattern) for pattern in self.allowed_tools)


class RuleBasedAccessController:
    """按顺序匹配允许规则、默认拒绝的访问控制器。

    通过 ``agent_id`` / ``user_id`` / ``tenant_id`` 三个维度的精确匹配实现
    Agent、用户与租户隔离：规则中声明的身份维度不匹配时调用不会被放行。

    Args:
        rules: 允许型规则序列；为空时拒绝一切调用。
    """

    def __init__(self, rules: Iterable[AccessRule] = ()) -> None:
        self._rules = tuple(rules)

    async def authorize(
        self,
        principal: Principal,
        tool_name: str,
        arguments: Mapping[str, object],
    ) -> AccessDecision:
        """返回第一条命中规则的允许决策；无命中时默认拒绝。

        Args:
            principal: 发起调用的主体。
            tool_name: 工具注册名称。
            arguments: 结构化工具参数；规则匹配不依赖参数内容。

        Returns:
            带原因说明的访问决策。
        """
        del arguments
        for index, rule in enumerate(self._rules):
            if rule.matches(principal, tool_name):
                return AccessDecision(True, f"access rule {index} allows tool {tool_name}")
        return AccessDecision(False, f"no access rule allows tool {tool_name}")
