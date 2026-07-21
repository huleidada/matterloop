"""工具注册、授权和热替换测试。"""

import asyncio
from collections.abc import Mapping

import pytest
from matterloop_tools import (
    PermissionDecision,
    ToolAccessScope,
    ToolContext,
    ToolEffect,
    ToolPermissionDeniedError,
    ToolRegistry,
    ToolResult,
    ToolSpec,
)


class EchoTool:
    """返回构造标签的测试工具。"""

    def __init__(self, label: str) -> None:
        self.label = label
        self.closed = False
        self._spec = ToolSpec("echo", "回显测试参数", {"type": "object"})

    @property
    def spec(self) -> ToolSpec:
        return self._spec

    async def invoke(
        self,
        arguments: Mapping[str, object],
        context: ToolContext,
    ) -> ToolResult:
        del arguments, context
        return ToolResult(self.label)

    async def aclose(self) -> None:
        self.closed = True


class DenyAuthorizer:
    """拒绝所有调用的测试授权器。"""

    async def authorize(
        self,
        tool_name: str,
        arguments: Mapping[str, object],
        context: ToolContext,
    ) -> PermissionDecision:
        del tool_name, arguments, context
        return PermissionDecision.DENY


class PausingAuthorizer:
    """暂停授权，模拟调用方并发修改原始参数。"""

    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.resume = asyncio.Event()
        self.observed: Mapping[str, object] = {}

    async def authorize(
        self,
        tool_name: str,
        arguments: Mapping[str, object],
        context: ToolContext,
    ) -> PermissionDecision:
        del tool_name, context
        self.started.set()
        await self.resume.wait()
        self.observed = arguments
        return PermissionDecision.ALLOW


class CountingAuthorizer:
    """记录授权次数，用于验证强制边界先于可插拔策略。"""

    def __init__(self) -> None:
        self.calls = 0

    async def authorize(
        self,
        tool_name: str,
        arguments: Mapping[str, object],
        context: ToolContext,
    ) -> PermissionDecision:
        del tool_name, arguments, context
        self.calls += 1
        return PermissionDecision.ALLOW


class EffectTool:
    """暴露指定副作用分类并记录是否实际执行。"""

    def __init__(self, spec: ToolSpec) -> None:
        self._spec = spec
        self.invocations = 0

    @property
    def spec(self) -> ToolSpec:
        return self._spec

    async def invoke(
        self,
        arguments: Mapping[str, object],
        context: ToolContext,
    ) -> ToolResult:
        del arguments, context
        self.invocations += 1
        return ToolResult("ok")


class ArgumentTool:
    """返回调用参数中 value 字段的测试工具。"""

    def __init__(self, name: str) -> None:
        self._spec = ToolSpec(name, "返回稳定参数", {"type": "object"})

    @property
    def spec(self) -> ToolSpec:
        return self._spec

    async def invoke(
        self,
        arguments: Mapping[str, object],
        context: ToolContext,
    ) -> ToolResult:
        del context
        return ToolResult(str(arguments["value"]))


class ContextTool:
    """返回上下文中嵌套租户标识的测试工具。"""

    spec = ToolSpec("context", "返回稳定上下文", {"type": "object"})

    async def invoke(
        self,
        arguments: Mapping[str, object],
        context: ToolContext,
    ) -> ToolResult:
        del arguments
        identity = context.metadata["identity"]
        assert isinstance(identity, Mapping)
        return ToolResult(str(identity["tenant"]))


async def test_registry_invokes_and_replaces_tool() -> None:
    old = EchoTool("old")
    new = EchoTool("new")
    registry = ToolRegistry([old])
    context = ToolContext("run")

    assert registry.names() == ("echo",)
    assert (await registry.invoke("echo", {}, context=context)).content == "old"
    await registry.replace("echo", new)
    assert old.closed
    assert (await registry.invoke("echo", {}, context=context)).content == "new"


async def test_registry_checks_authorization_before_invocation() -> None:
    registry = ToolRegistry([EchoTool("hidden")], authorizer=DenyAuthorizer())

    with pytest.raises(ToolPermissionDeniedError):
        await registry.invoke("echo", {}, context=ToolContext("run"))


async def test_registry_uses_same_argument_snapshot_for_authorization_and_invocation() -> None:
    authorizer = PausingAuthorizer()
    registry = ToolRegistry([ArgumentTool("argument")], authorizer=authorizer)
    arguments: dict[str, object] = {"value": "original"}

    invocation = asyncio.create_task(
        registry.invoke("argument", arguments, context=ToolContext("run"))
    )
    await authorizer.started.wait()
    arguments["value"] = "modified"
    authorizer.resume.set()

    result = await invocation

    assert authorizer.observed["value"] == "original"
    assert result.content == "original"


async def test_registry_recursively_snapshots_nested_arguments() -> None:
    authorizer = PausingAuthorizer()
    registry = ToolRegistry([ArgumentTool("argument")], authorizer=authorizer)
    arguments: dict[str, object] = {"value": ["original"]}

    invocation = asyncio.create_task(
        registry.invoke("argument", arguments, context=ToolContext("run"))
    )
    await authorizer.started.wait()
    nested = arguments["value"]
    assert isinstance(nested, list)
    nested[0] = "modified"
    authorizer.resume.set()

    result = await invocation

    assert authorizer.observed["value"] == ["original"]
    assert result.content == "['original']"


async def test_tool_context_recursively_snapshots_nested_metadata() -> None:
    authorizer = PausingAuthorizer()
    registry = ToolRegistry([ContextTool()], authorizer=authorizer)
    identity: dict[str, object] = {"tenant": "tenant-a"}
    context = ToolContext("run", metadata={"identity": identity})

    invocation = asyncio.create_task(registry.invoke("context", {}, context=context))
    await authorizer.started.wait()
    identity["tenant"] = "tenant-b"
    authorizer.resume.set()

    result = await invocation

    assert result.content == "tenant-a"


async def test_registry_pins_tool_lease_before_authorization() -> None:
    authorizer = PausingAuthorizer()
    old = EchoTool("old-safe")
    new = EchoTool("new-version")
    registry = ToolRegistry([old], authorizer=authorizer)

    invocation = asyncio.create_task(registry.invoke("echo", {}, context=ToolContext("run")))
    await authorizer.started.wait()
    await registry.replace("echo", new)

    assert not old.closed
    authorizer.resume.set()
    old_result = await invocation
    new_result = await registry.invoke("echo", {}, context=ToolContext("run"))

    assert old_result.content == "old-safe"
    assert new_result.content == "new-version"
    assert old.closed


def test_registry_returns_specs_in_stable_name_order() -> None:
    registry = ToolRegistry([ArgumentTool("zeta"), ArgumentTool("alpha")])

    assert tuple(spec.name for spec in registry.specs()) == ("alpha", "zeta")


async def test_read_only_scope_denies_unknown_effect_before_authorizer() -> None:
    authorizer = CountingAuthorizer()
    tool = EffectTool(ToolSpec("unknown", "未知副作用", {"type": "object"}))
    registry = ToolRegistry([tool], authorizer=authorizer)

    with pytest.raises(ToolPermissionDeniedError):
        await registry.invoke(
            "unknown",
            {},
            context=ToolContext("child", access_scope=ToolAccessScope.READ_ONLY),
        )

    assert authorizer.calls == 0
    assert tool.invocations == 0


async def test_read_only_scope_allows_explicit_read_effect() -> None:
    authorizer = CountingAuthorizer()
    tool = EffectTool(
        ToolSpec(
            "reader",
            "只读工具",
            {"type": "object"},
            default_effect=ToolEffect.READ,
        )
    )
    registry = ToolRegistry([tool], authorizer=authorizer)

    result = await registry.invoke(
        "reader",
        {},
        context=ToolContext("child", access_scope=ToolAccessScope.READ_ONLY),
    )

    assert result.content == "ok"
    assert authorizer.calls == 1
    assert tool.invocations == 1


async def test_full_scope_keeps_main_loop_access_to_unknown_effect() -> None:
    tool = EffectTool(ToolSpec("main", "主 Loop 工具", {"type": "object"}))
    registry = ToolRegistry([tool])

    result = await registry.invoke("main", {}, context=ToolContext("main"))

    assert result.content == "ok"
    assert tool.invocations == 1


def test_tool_spec_resolves_operation_effect_case_insensitively() -> None:
    spec = ToolSpec(
        "operation-tool",
        "按操作分类",
        {"type": "object"},
        default_effect=ToolEffect.UNKNOWN,
        effect_argument="operation",
        effect_mapping={"read": ToolEffect.READ, "write": ToolEffect.WRITE},
    )

    assert spec.effect_for({"operation": "READ"}) is ToolEffect.READ
    assert spec.effect_for({"operation": "write"}) is ToolEffect.WRITE
    assert spec.effect_for({"operation": "delete"}) is ToolEffect.UNKNOWN
