"""身份认证、角色授权与数据访问控制测试。"""

from __future__ import annotations

import asyncio
import hashlib

import pytest
from matterloop_policies import (
    AuthenticationError,
    Authenticator,
    AuthorizationError,
    Authorizer,
    DataAccessPolicy,
    DataAccessRule,
    Grant,
    Identity,
    RoleBasedAuthorizer,
    StaticTokenAuthenticator,
)

_ALICE = Identity("alice", tenant_id="acme", roles=("analyst",))


def test_static_token_authenticator_resolves_known_tokens() -> None:
    """已注册 token 应换回对应身份，未知 token 必须失败。"""

    async def scenario() -> None:
        authenticator: Authenticator = StaticTokenAuthenticator({"token-alice": _ALICE})

        identity = await authenticator.authenticate("token-alice")
        assert identity is _ALICE
        with pytest.raises(AuthenticationError):
            await authenticator.authenticate("token-mallory")

    asyncio.run(scenario())


def test_static_token_authenticator_stores_digest_instead_of_plaintext() -> None:
    """认证器内部只能保存 sha256 摘要，不得保留 token 原文。"""
    token = "token-alice"
    authenticator = StaticTokenAuthenticator({token: _ALICE})

    entries = authenticator._entries
    assert entries == ((hashlib.sha256(token.encode("utf-8")).digest(), _ALICE),)
    for attribute in vars(authenticator).values():
        assert token not in repr(attribute)


def test_identity_freezes_metadata_and_rejects_empty_principal() -> None:
    """身份元数据必须只读，主体标识不能为空。"""
    identity = Identity("alice", metadata={"team": "materials"})

    with pytest.raises(TypeError):
        identity.metadata["team"] = "override"  # type: ignore[index]
    with pytest.raises(ValueError):
        Identity("  ")


def test_role_based_authorizer_matches_wildcards_and_denies_by_default() -> None:
    """角色规则支持通配匹配，未命中与未知角色一律拒绝。"""

    async def scenario() -> None:
        authorizer: Authorizer = RoleBasedAuthorizer(
            {"analyst": (Grant(action="run:*", resource="checkpoint/*"),)}
        )

        assert await authorizer.authorize(_ALICE, "run:create", "checkpoint/run-1")
        assert not await authorizer.authorize(_ALICE, "admin:delete", "checkpoint/run-1")
        assert not await authorizer.authorize(_ALICE, "run:create", "secrets/run-1")
        outsider = Identity("bob", tenant_id="acme", roles=("viewer",))
        assert not await authorizer.authorize(outsider, "run:create", "checkpoint/run-1")

    asyncio.run(scenario())


def test_role_based_authorizer_require_raises_on_denial() -> None:
    """require 在授权通过时静默返回，拒绝时抛出领域异常。"""

    async def scenario() -> None:
        authorizer = RoleBasedAuthorizer({"analyst": (Grant(action="run:read", resource="run/*"),)})

        await authorizer.require(_ALICE, "run:read", "run/run-1")
        with pytest.raises(AuthorizationError):
            await authorizer.require(_ALICE, "run:delete", "run/run-1")

    asyncio.run(scenario())


def test_data_access_policy_denies_cross_tenant_by_default() -> None:
    """规则命中但租户不一致时必须拒绝，缺少租户的身份同样拒绝。"""
    policy = DataAccessPolicy(
        (DataAccessRule("acme", "checkpoint/*", frozenset({"read", "write"})),)
    )

    assert policy.check(_ALICE, "read", "checkpoint/run-1")
    assert policy.check(_ALICE, "write", "checkpoint/run-1")
    assert not policy.check(_ALICE, "delete", "checkpoint/run-1")
    assert not policy.check(_ALICE, "read", "secrets/run-1")
    outsider = Identity("eve", tenant_id="globex", roles=("analyst",))
    assert not policy.check(outsider, "read", "checkpoint/run-1")
    platform = Identity("svc", tenant_id=None)
    assert not policy.check(platform, "read", "checkpoint/run-1")


def test_data_access_policy_allows_explicit_cross_tenant_rules() -> None:
    """显式 allow_cross_tenant 的规则可放行其他租户的身份。"""
    policy = DataAccessPolicy(
        (
            DataAccessRule(
                "acme",
                "public/*",
                frozenset({"read"}),
                allow_cross_tenant=True,
            ),
        )
    )
    outsider = Identity("eve", tenant_id="globex")

    assert policy.check(outsider, "read", "public/report")
    assert not policy.check(outsider, "write", "public/report")
