"""工具注册、授权和热替换测试。"""

from collections.abc import Mapping

import pytest
from matterloop_tools import (
    PermissionDecision,
    ToolContext,
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
