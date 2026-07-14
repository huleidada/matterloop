"""支持生命周期与无中断热替换的运行时组件容器。"""

from __future__ import annotations

import inspect
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Generic, TypeVar, cast

from matterloop_runtime.errors import (
    ComponentExistsError,
    ComponentNotFoundError,
    RuntimeClosedError,
)

T = TypeVar("T")


@dataclass(slots=True)
class _ComponentSlot(Generic[T]):
    component: T
    active_calls: int = 0
    retired: bool = False


class RuntimeContainer(Generic[T]):
    """管理组件启动、借用、热替换与异步关闭。

    新组件只有在 ``start`` 成功后才会原子替换旧组件。已经借用旧组件的调用可以继续
    完成；最后一个旧调用退出后，容器才调用旧组件的 ``aclose``。
    """

    def __init__(self, components: Mapping[str, T] | None = None) -> None:
        import asyncio

        self._slots: dict[str, _ComponentSlot[T]] = {
            name: _ComponentSlot(component) for name, component in (components or {}).items()
        }
        self._lock = asyncio.Lock()
        self._closed = False

    async def register(self, name: str, component: T, *, replace: bool = False) -> None:
        """注册组件，并在允许时安全替换同名组件。

        Args:
            name: 唯一组件名称。
            component: 需要托管的组件。
            replace: 同名组件存在时是否替换。

        Raises:
            ComponentExistsError: 同名组件存在且未允许替换。
            RuntimeClosedError: 容器已关闭。
        """
        if not name.strip():
            raise ValueError("component name must not be empty")
        try:
            await self._start(component)
        except Exception:
            # start 可能已经分配了部分资源；尽力关闭新实例，旧实例始终保持可用。
            await self._close(component)
            raise
        old_slot: _ComponentSlot[T] | None = None
        try:
            async with self._lock:
                if self._closed:
                    raise RuntimeClosedError("runtime container is closed")
                if name in self._slots and not replace:
                    raise ComponentExistsError(name)
                old_slot = self._slots.get(name)
                self._slots[name] = _ComponentSlot(component)
                if old_slot is not None:
                    old_slot.retired = True
        except Exception:
            await self._close(component)
            raise
        if old_slot is not None and old_slot.active_calls == 0:
            await self._close(old_slot.component)

    async def replace(self, name: str, component: T) -> None:
        """原子热替换一个已注册组件。

        Args:
            name: 已注册组件名称。
            component: 新组件。

        Raises:
            ComponentNotFoundError: 目标组件不存在。
        """
        async with self._lock:
            if name not in self._slots:
                raise ComponentNotFoundError(name)
        await self.register(name, component, replace=True)

    async def unregister(self, name: str) -> None:
        """移除组件，并在所有旧调用结束后关闭它。

        Args:
            name: 需要移除的组件名称。
        """
        async with self._lock:
            slot = self._slots.pop(name, None)
            if slot is None:
                raise ComponentNotFoundError(name)
            slot.retired = True
        if slot.active_calls == 0:
            await self._close(slot.component)

    def get(self, name: str) -> T:
        """返回当前组件；长时间调用应优先使用 ``acquire``。

        Args:
            name: 组件名称。

        Returns:
            当前注册组件。

        Raises:
            ComponentNotFoundError: 组件不存在。
            RuntimeClosedError: 容器已关闭。
        """
        if self._closed:
            raise RuntimeClosedError("runtime container is closed")
        try:
            return self._slots[name].component
        except KeyError as exc:
            raise ComponentNotFoundError(name) from exc

    def names(self) -> tuple[str, ...]:
        """返回稳定排序的组件名称。"""
        return tuple(sorted(self._slots))

    @asynccontextmanager
    async def acquire(self, name: str) -> AsyncIterator[T]:
        """在一次调用期间固定并借用当前组件。

        Args:
            name: 组件名称。

        Yields:
            当前调用应使用的组件实例。
        """
        async with self._lock:
            if self._closed:
                raise RuntimeClosedError("runtime container is closed")
            slot = self._slots.get(name)
            if slot is None:
                raise ComponentNotFoundError(name)
            slot.active_calls += 1
        try:
            yield slot.component
        finally:
            should_close = False
            async with self._lock:
                slot.active_calls -= 1
                should_close = slot.retired and slot.active_calls == 0
            if should_close:
                await self._close(slot.component)

    async def aclose(self) -> None:
        """禁止新调用并关闭当前空闲组件。

        已在执行的调用会在退出 ``acquire`` 后关闭对应组件，关闭操作不会中断它们。
        """
        async with self._lock:
            if self._closed:
                return
            self._closed = True
            slots = tuple(self._slots.values())
            self._slots.clear()
            for slot in slots:
                slot.retired = True
        for slot in slots:
            if slot.active_calls == 0:
                await self._close(slot.component)

    @staticmethod
    async def _start(component: T) -> None:
        method = getattr(component, "start", None)
        if method is None:
            return
        result = cast(Callable[[], object], method)()
        if inspect.isawaitable(result):
            await cast(Awaitable[object], result)

    @staticmethod
    async def _close(component: T) -> None:
        method = getattr(component, "aclose", None)
        if method is None:
            return
        result = cast(Callable[[], object], method)()
        if inspect.isawaitable(result):
            await cast(Awaitable[object], result)
