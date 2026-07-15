"""权限感知且支持安全热替换的工具注册表。"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from math import isfinite
from types import MappingProxyType

from matterloop_runtime import (
    ComponentExistsError,
    ComponentNotFoundError,
    RuntimeContainer,
)

from matterloop_tools.base import (
    AllowAllToolAuthorizer,
    PermissionDecision,
    Tool,
    ToolAuthorizer,
    ToolContext,
    ToolResult,
    ToolSpec,
)
from matterloop_tools.errors import (
    ToolInputError,
    ToolNotFoundError,
    ToolPermissionDeniedError,
)

_MAX_ARGUMENT_DEPTH = 64


class ToolRegistry:
    """统一负责工具发现、授权、调用与生命周期。

    Args:
        tools: 无需异步启动的初始工具。
        authorizer: 每次调用前执行的授权器；默认显式放行。
    """

    def __init__(
        self,
        tools: Iterable[Tool] = (),
        *,
        authorizer: ToolAuthorizer | None = None,
    ) -> None:
        initial: dict[str, Tool] = {}
        for tool in tools:
            if tool.spec.name in initial:
                raise ComponentExistsError(tool.spec.name)
            initial[tool.spec.name] = tool
        self._components: RuntimeContainer[Tool] = RuntimeContainer(initial)
        self._authorizer = authorizer or AllowAllToolAuthorizer()

    async def register(self, tool: Tool, *, replace: bool = False) -> None:
        """注册工具，并在允许时安全替换同名实例。

        Args:
            tool: 需要注册的工具。
            replace: 是否替换同名工具。
        """
        await self._components.register(tool.spec.name, tool, replace=replace)

    async def replace(self, name: str, tool: Tool) -> None:
        """安全热替换一个工具。

        Args:
            name: 需要替换的注册名称。
            tool: 新工具实例，其规范名称必须与注册名称一致。
        """
        if tool.spec.name != name:
            raise ValueError("replacement tool name must match registry name")
        await self._components.replace(name, tool)

    async def unregister(self, name: str) -> None:
        """移除并安全关闭一个工具。"""
        try:
            await self._components.unregister(name)
        except ComponentNotFoundError as exc:
            raise ToolNotFoundError(name) from exc

    def get(self, name: str) -> Tool:
        """返回当前工具，主要用于读取稳定发现信息。"""
        try:
            return self._components.get(name)
        except ComponentNotFoundError as exc:
            raise ToolNotFoundError(name) from exc

    def names(self) -> tuple[str, ...]:
        """返回稳定排序的工具名称。"""
        return self._components.names()

    def specs(self) -> tuple[ToolSpec, ...]:
        """返回当前工具的稳定排序发现信息快照。

        Returns:
            按工具名称排序的不可变 ``ToolSpec`` 元组。

        Notes:
            单个工具热替换是原子的；若调用方同时替换多个工具，本方法不承诺跨名称的
            事务快照。需要目录级原子性的能力应由上层注册表提供不可变目录版本。
        """
        return tuple(self._components.get(name).spec for name in self._components.names())

    async def invoke(
        self,
        name: str,
        arguments: Mapping[str, object],
        *,
        context: ToolContext,
    ) -> ToolResult:
        """授权并调用执行期间固定的工具实例。

        Args:
            name: 工具注册名称。
            arguments: 结构化工具参数。
            context: 运行和步骤上下文。

        Returns:
            工具标准结果。

        Raises:
            ToolPermissionDeniedError: 授权器拒绝调用。
            ToolNotFoundError: 工具不存在。
        """
        stable_arguments = _freeze_arguments(arguments)
        try:
            async with self._components.acquire(name) as tool:
                # 租约覆盖授权与执行，避免同名工具在授权后被替换成不同实现。
                decision = await self._authorizer.authorize(
                    name,
                    _thaw_arguments(stable_arguments),
                    context,
                )
                if decision is not PermissionDecision.ALLOW:
                    raise ToolPermissionDeniedError(name)
                return await tool.invoke(_thaw_arguments(stable_arguments), context)
        except ComponentNotFoundError as exc:
            raise ToolNotFoundError(name) from exc

    async def aclose(self) -> None:
        """关闭注册表并释放所有空闲工具。"""
        await self._components.aclose()


def _freeze_arguments(arguments: Mapping[str, object]) -> Mapping[str, object]:
    """复制并递归冻结 JSON 兼容工具参数。"""
    active: set[int] = set()
    frozen = _freeze_value(arguments, depth=0, active=active)
    if not isinstance(frozen, Mapping):
        raise ToolInputError("tool arguments must be a JSON object")
    return frozen


def _freeze_value(value: object, *, depth: int, active: set[int]) -> object:
    if depth > _MAX_ARGUMENT_DEPTH:
        raise ToolInputError("tool arguments exceed maximum nesting depth")
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if not isfinite(value):
            raise ToolInputError("tool arguments must not contain non-finite numbers")
        return value
    if isinstance(value, Mapping):
        identity = id(value)
        if identity in active:
            raise ToolInputError("tool arguments must not contain cycles")
        active.add(identity)
        try:
            frozen_mapping: dict[str, object] = {}
            for key, item in value.items():
                if not isinstance(key, str):
                    raise ToolInputError("tool argument object keys must be strings")
                frozen_mapping[key] = _freeze_value(item, depth=depth + 1, active=active)
            return MappingProxyType(frozen_mapping)
        finally:
            active.remove(identity)
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        identity = id(value)
        if identity in active:
            raise ToolInputError("tool arguments must not contain cycles")
        active.add(identity)
        try:
            return tuple(_freeze_value(item, depth=depth + 1, active=active) for item in value)
        finally:
            active.remove(identity)
    raise ToolInputError("tool arguments must contain only JSON-compatible values")


def _thaw_arguments(arguments: Mapping[str, object]) -> Mapping[str, object]:
    """为工具创建与已授权快照等值的独立可变参数。"""
    thawed = _thaw_value(arguments)
    if not isinstance(thawed, dict):
        raise ToolInputError("tool arguments must be a JSON object")
    return thawed


def _thaw_value(value: object) -> object:
    if isinstance(value, Mapping):
        return {key: _thaw_value(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw_value(item) for item in value]
    return value
