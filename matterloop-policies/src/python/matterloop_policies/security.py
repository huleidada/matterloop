"""提供身份认证、角色授权与租户级数据访问控制。"""

from __future__ import annotations

import hashlib
import hmac
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from fnmatch import fnmatchcase
from types import MappingProxyType
from typing import Protocol, runtime_checkable


class AuthenticationError(Exception):
    """凭据无法映射到任何已知身份。

    异常消息不包含凭据原文或其摘要。
    """


class AuthorizationError(Exception):
    """身份没有对目标资源执行目标操作的权限。"""


@dataclass(frozen=True, slots=True)
class Identity:
    """描述一个通过认证的请求主体。

    Args:
        principal_id: 全局唯一的主体标识，例如用户或服务账号。
        tenant_id: 主体归属的租户，平台级主体可为 ``None``。
        roles: 主体持有的角色名集合。
        metadata: 由宿主注入的附加元数据，构造后不可修改。
    """

    principal_id: str
    tenant_id: str | None = None
    roles: tuple[str, ...] = ()
    metadata: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        """拒绝空主体标识并冻结元数据。"""
        if not self.principal_id.strip():
            raise ValueError("principal id must not be empty")
        object.__setattr__(self, "metadata", MappingProxyType(dict(self.metadata)))


@runtime_checkable
class Authenticator(Protocol):
    """把外部凭据换成内部身份的结构协议。"""

    async def authenticate(self, credential: str) -> Identity:
        """认证一个凭据并返回对应身份。

        Args:
            credential: 调用方出示的不透明凭据。

        Returns:
            凭据对应的身份。

        Raises:
            AuthenticationError: 凭据未命中任何已知身份。
        """
        ...


class StaticTokenAuthenticator:
    """基于静态 token 映射的认证器。

    构造后内部只保存 token 的 SHA-256 摘要，不保留原文；认证时用
    ``hmac.compare_digest`` 逐条比较摘要以抵御时序攻击。适合测试
    与小规模部署，生产环境应替换为对接企业 IdP 的实现。
    """

    def __init__(self, tokens: Mapping[str, Identity]) -> None:
        entries: list[tuple[bytes, Identity]] = []
        for token, identity in tokens.items():
            if not token:
                raise ValueError("token must not be empty")
            entries.append((hashlib.sha256(token.encode("utf-8")).digest(), identity))
        self._entries = tuple(entries)

    async def authenticate(self, credential: str) -> Identity:
        """比较凭据摘要并返回命中的身份。

        Args:
            credential: 调用方出示的静态 token。

        Returns:
            token 对应的身份。

        Raises:
            AuthenticationError: token 未命中任何已知身份。
        """
        digest = hashlib.sha256(credential.encode("utf-8")).digest()
        matched: Identity | None = None
        # 始终遍历全部条目，避免命中位置影响耗时。
        for stored_digest, identity in self._entries:
            if hmac.compare_digest(stored_digest, digest):
                matched = identity
        if matched is None:
            raise AuthenticationError("credential is not recognized")
        return matched


@runtime_checkable
class Authorizer(Protocol):
    """判断身份能否对资源执行操作的结构协议。"""

    async def authorize(self, identity: Identity, action: str, resource: str) -> bool:
        """判断一次访问是否被授权。

        Args:
            identity: 已通过认证的请求主体。
            action: 目标操作名称，例如 ``run:create``。
            resource: 目标资源标识，例如 ``checkpoint/run-1``。

        Returns:
            允许访问时返回 ``True``。
        """
        ...


@dataclass(frozen=True, slots=True)
class Grant:
    """定义一条角色授权规则。

    ``action`` 与 ``resource`` 均为 :func:`fnmatch.fnmatchcase`
    通配模式，例如 ``run:*`` 或 ``checkpoint/*``。
    """

    action: str
    resource: str

    def matches(self, action: str, resource: str) -> bool:
        """判断一次访问是否命中本条规则。"""
        return fnmatchcase(action, self.action) and fnmatchcase(resource, self.resource)


class RoleBasedAuthorizer:
    """默认拒绝的角色授权器。

    只有身份持有的某个角色存在命中的 :class:`Grant` 时才放行；
    未知角色与未命中规则一律拒绝。
    """

    def __init__(self, grants: Mapping[str, Iterable[Grant]]) -> None:
        self._grants: dict[str, tuple[Grant, ...]] = {
            role: tuple(items) for role, items in grants.items()
        }

    async def authorize(self, identity: Identity, action: str, resource: str) -> bool:
        """按身份角色依次匹配授权规则。"""
        for role in identity.roles:
            for grant in self._grants.get(role, ()):
                if grant.matches(action, resource):
                    return True
        return False

    async def require(self, identity: Identity, action: str, resource: str) -> None:
        """要求一次访问必须通过授权。

        Args:
            identity: 已通过认证的请求主体。
            action: 目标操作名称。
            resource: 目标资源标识。

        Raises:
            AuthorizationError: 授权检查未通过。
        """
        if not await self.authorize(identity, action, resource):
            raise AuthorizationError(
                f"identity {identity.principal_id!r} is not allowed to "
                f"perform {action!r} on {resource!r}"
            )


@dataclass(frozen=True, slots=True)
class DataAccessRule:
    """定义一条资源级数据访问规则。

    Args:
        tenant_id: 规则覆盖资源的归属租户。
        resource: 资源标识的 fnmatch 通配模式。
        actions: 允许的操作名集合。
        allow_cross_tenant: 显式允许其他租户的身份命中本规则。
    """

    tenant_id: str
    resource: str
    actions: frozenset[str]
    allow_cross_tenant: bool = False

    def __post_init__(self) -> None:
        """拒绝空租户与空操作集合。"""
        if not self.tenant_id.strip():
            raise ValueError("rule tenant id must not be empty")
        if not self.actions:
            raise ValueError("rule must allow at least one action")


class DataAccessPolicy:
    """默认拒绝的租户级数据访问策略。

    只有存在同租户的命中规则时才放行；跨租户访问一律拒绝，
    除非规则显式携带 ``allow_cross_tenant=True``。
    """

    def __init__(self, rules: Iterable[DataAccessRule] = ()) -> None:
        self._rules = tuple(rules)

    def check(self, identity: Identity, action: str, resource: str) -> bool:
        """判断身份能否对资源执行操作。

        Args:
            identity: 已通过认证的请求主体。
            action: 目标操作名称。
            resource: 目标资源标识。

        Returns:
            存在命中规则且租户校验通过时返回 ``True``。
        """
        for rule in self._rules:
            if action not in rule.actions:
                continue
            if not fnmatchcase(resource, rule.resource):
                continue
            if rule.allow_cross_tenant:
                return True
            if identity.tenant_id is not None and identity.tenant_id == rule.tenant_id:
                return True
        return False


__all__ = [
    "AuthenticationError",
    "Authenticator",
    "AuthorizationError",
    "Authorizer",
    "DataAccessPolicy",
    "DataAccessRule",
    "Grant",
    "Identity",
    "RoleBasedAuthorizer",
    "StaticTokenAuthenticator",
]
