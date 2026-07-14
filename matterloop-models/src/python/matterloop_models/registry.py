"""提供线程安全、支持能力描述与安全热替换的模型注册表。"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass, field
from threading import Event, RLock
from weakref import finalize

from matterloop_models.base import ModelClient
from matterloop_models.capabilities import ModelDescriptor
from matterloop_models.errors import ModelAlreadyRegisteredError, ModelNotFoundError


@dataclass(slots=True)
class _ModelSlot:
    client: ModelClient
    descriptor: ModelDescriptor | None = None
    active_leases: int = 0
    retired: bool = False
    drained: Event = field(default_factory=Event)


class ModelLease:
    """在一个完整模型事务内持有查询时刻的客户端快照。

    租约同时支持同步和异步上下文管理器。离开上下文会通知注册表释放
    活跃引用，但不会擅自关闭由组合根管理的客户端。
    """

    def __init__(
        self,
        client: ModelClient,
        release_callback: Callable[[], None] | None = None,
    ) -> None:
        self._client: ModelClient | None = client
        self._entered = False
        self._lock = RLock()
        self._finalizer = None if release_callback is None else finalize(self, release_callback)

    def __enter__(self) -> ModelClient:
        """进入同步租约并返回固定客户端。"""
        return self._enter()

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: object,
    ) -> None:
        """释放同步租约，不拦截事务异常。"""
        del exc_type, exc_value, traceback
        self.release()

    async def __aenter__(self) -> ModelClient:
        """进入异步租约并返回固定客户端。"""
        return self._enter()

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: object,
    ) -> None:
        """释放异步租约，不拦截事务异常。"""
        del exc_type, exc_value, traceback
        self.release()

    def release(self) -> None:
        """幂等释放租约，也可用于释放尚未进入上下文的快照。"""
        with self._lock:
            if self._client is None:
                return
            self._client = None
            finalizer = self._finalizer
        if finalizer is not None and finalizer.alive:
            finalizer()

    async def aclose(self) -> None:
        """以异步资源接口幂等释放租约。"""
        self.release()

    def _enter(self) -> ModelClient:
        with self._lock:
            if self._entered:
                raise RuntimeError("model lease cannot be entered more than once")
            client = self._client
            if client is None:
                raise RuntimeError("model lease has already been released")
            self._entered = True
            return client


class ModelRetirement:
    """表示已从注册表移除、正等待旧租约排空的客户端。

    注册表不拥有客户端资源。调用方应先等待 :meth:`wait_drained`，
    再按自身所有权策略关闭 :attr:`client`。
    """

    def __init__(self, slot: _ModelSlot) -> None:
        self._slot = slot

    @property
    def client(self) -> ModelClient:
        """返回已退役但尚未由注册表关闭的客户端。"""
        return self._slot.client

    @property
    def descriptor(self) -> ModelDescriptor | None:
        """返回退役客户端注册时的可选描述。"""
        return self._slot.descriptor

    @property
    def is_drained(self) -> bool:
        """返回所有在替换或移除前取得的租约是否已释放。"""
        return self._slot.drained.is_set()

    async def wait_drained(self) -> ModelClient:
        """异步等待退役客户端不再被任何租约使用。

        Returns:
            已安全排空、可由调用方关闭的客户端。
        """
        if not self._slot.drained.is_set():
            await asyncio.to_thread(self._slot.drained.wait)
        return self._slot.client


class ModelRegistry:
    """保存具名模型客户端，并让后续查询立即看到热替换结果。"""

    def __init__(self) -> None:
        self._slots: dict[str, _ModelSlot] = {}
        self._lock = RLock()

    def register(
        self,
        name: str,
        client: ModelClient,
        *,
        replace: bool = False,
        descriptor: ModelDescriptor | None = None,
    ) -> None:
        """注册或原子替换模型客户端。

        Args:
            name: 供 Agent 配置引用的稳定名称。
            client: 满足模型调用协议的实例。
            replace: 是否允许替换已有同名客户端。
            descriptor: 可选的非敏感模型描述；省略时尝试读取客户端的 ``descriptor``。

        Raises:
            ValueError: 模型名称为空。
            ModelAlreadyRegisteredError: 名称重复且未允许替换。

        Notes:
            该方法保留外部生命周期管理语义，不会关闭被替换的客户端。
            需要等待旧调用完成时应使用 :meth:`swap`。
        """
        normalized_name = self._normalize_name(name)
        resolved_descriptor = self._resolve_descriptor(client, descriptor)
        with self._lock:
            old_slot = self._slots.get(normalized_name)
            if old_slot is not None and not replace:
                raise ModelAlreadyRegisteredError(normalized_name)
            self._slots[normalized_name] = _ModelSlot(client, resolved_descriptor)
            if old_slot is not None:
                self._retire_slot(old_slot)

    def get(self, name: str) -> ModelClient:
        """返回查询时刻的模型客户端快照。

        Raises:
            ModelNotFoundError: 注册表中不存在指定名称。
        """
        normalized_name = self._normalize_name(name)
        with self._lock:
            try:
                return self._slots[normalized_name].client
            except KeyError as exc:
                raise ModelNotFoundError(normalized_name) from exc

    def describe(self, name: str) -> ModelDescriptor | None:
        """返回注册时显式提供或从客户端推断的模型描述。

        Args:
            name: 已注册模型的稳定名称。

        Returns:
            非敏感描述；客户端没有提供时返回 ``None``。

        Raises:
            ModelNotFoundError: 注册表中不存在指定名称。
        """
        normalized_name = self._normalize_name(name)
        with self._lock:
            try:
                return self._slots[normalized_name].descriptor
            except KeyError as exc:
                raise ModelNotFoundError(normalized_name) from exc

    def acquire(self, name: str) -> ModelLease:
        """原子获取一个可跨多次异步调用使用的客户端租约。

        调用该方法时即固定当前客户端并计入活跃租约；后续热替换只影响
        新租约。调用方应立即进入上下文，或对不再使用的租约调用
        :meth:`ModelLease.release`。

        Args:
            name: 已注册模型的稳定名称。

        Returns:
            同时支持 ``with`` 和 ``async with`` 的客户端租约。

        Raises:
            ModelNotFoundError: 注册表中不存在指定名称。
        """
        normalized_name = self._normalize_name(name)
        with self._lock:
            try:
                slot = self._slots[normalized_name]
            except KeyError as exc:
                raise ModelNotFoundError(normalized_name) from exc
            slot.active_leases += 1
            return ModelLease(slot.client, lambda: self._release_slot(slot))

    def swap(
        self,
        name: str,
        client: ModelClient,
        *,
        descriptor: ModelDescriptor | None = None,
    ) -> ModelRetirement:
        """原子换入新客户端，并返回可等待排空的旧客户端句柄。

        Args:
            name: 需要替换的已注册名称。
            client: 已由组合根构造并准备就绪的新客户端。
            descriptor: 新客户端的可选非敏感描述；省略时尝试从客户端推断。

        Returns:
            旧客户端的退役句柄。

        Raises:
            ModelNotFoundError: 目标名称不存在。
        """
        normalized_name = self._normalize_name(name)
        resolved_descriptor = self._resolve_descriptor(client, descriptor)
        with self._lock:
            try:
                old_slot = self._slots[normalized_name]
            except KeyError as exc:
                raise ModelNotFoundError(normalized_name) from exc
            self._slots[normalized_name] = _ModelSlot(client, resolved_descriptor)
            self._retire_slot(old_slot)
            return ModelRetirement(old_slot)

    def retire(self, name: str) -> ModelRetirement:
        """移除当前客户端，并返回可等待旧租约排空的句柄。

        Args:
            name: 需要安全移除的已注册名称。

        Returns:
            被移除客户端的退役句柄。

        Raises:
            ModelNotFoundError: 目标名称不存在。
        """
        normalized_name = self._normalize_name(name)
        with self._lock:
            try:
                slot = self._slots.pop(normalized_name)
            except KeyError as exc:
                raise ModelNotFoundError(normalized_name) from exc
            self._retire_slot(slot)
            return ModelRetirement(slot)

    def unregister(self, name: str) -> ModelClient:
        """立即移除并返回模型客户端，保留旧版外部清理语义。

        已取得的租约仍然保持客户端引用。若调用方需要在关闭前等待它们
        完成，应改用 :meth:`retire`。

        Raises:
            ModelNotFoundError: 注册表中不存在指定名称。
        """
        normalized_name = self._normalize_name(name)
        with self._lock:
            try:
                slot = self._slots.pop(normalized_name)
            except KeyError as exc:
                raise ModelNotFoundError(normalized_name) from exc
            self._retire_slot(slot)
            return slot.client

    def names(self) -> tuple[str, ...]:
        """返回排序后的模型名称快照。"""
        with self._lock:
            return tuple(sorted(self._slots))

    @staticmethod
    def _normalize_name(name: str) -> str:
        normalized_name = name.strip()
        if not normalized_name:
            raise ValueError("model name must not be empty")
        return normalized_name

    @staticmethod
    def _resolve_descriptor(
        client: ModelClient,
        descriptor: ModelDescriptor | None,
    ) -> ModelDescriptor | None:
        if descriptor is not None:
            return descriptor
        inferred = getattr(client, "descriptor", None)
        return inferred if isinstance(inferred, ModelDescriptor) else None

    @staticmethod
    def _retire_slot(slot: _ModelSlot) -> None:
        slot.retired = True
        if slot.active_leases == 0:
            slot.drained.set()

    def _release_slot(self, slot: _ModelSlot) -> None:
        with self._lock:
            if slot.active_leases < 1:
                return
            slot.active_leases -= 1
            if slot.retired and slot.active_leases == 0:
                slot.drained.set()


__all__ = ["ModelLease", "ModelRegistry", "ModelRetirement"]
