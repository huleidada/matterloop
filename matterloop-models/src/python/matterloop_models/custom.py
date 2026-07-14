"""提供将调用方异步函数封装为模型客户端的轻量适配器。"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from threading import RLock

from matterloop_models.base import ModelRequest, ModelResponse
from matterloop_models.capabilities import ModelDescriptor

ModelGenerateCallback = Callable[[ModelRequest], Awaitable[ModelResponse]]
ModelCloseCallback = Callable[[], Awaitable[None]]


class CallableModelClient:
    """把调用方提供的异步函数包装为通用模型客户端。

    Args:
        generate_callback: 接收通用请求并返回通用响应的异步函数。
        close_callback: 可选的异步资源关闭函数。
        descriptor: 可选的非敏感客户端描述，便于组合根注册。
    """

    def __init__(
        self,
        generate_callback: ModelGenerateCallback,
        *,
        close_callback: ModelCloseCallback | None = None,
        descriptor: ModelDescriptor | None = None,
    ) -> None:
        if not callable(generate_callback):
            raise TypeError("generate callback must be callable")
        if close_callback is not None and not callable(close_callback):
            raise TypeError("close callback must be callable")
        self._generate_callback = generate_callback
        self._close_callback = close_callback
        self._descriptor = descriptor
        self._closed = False
        self._lock = RLock()

    @property
    def descriptor(self) -> ModelDescriptor | None:
        """返回调用方显式提供的非敏感描述。"""
        return self._descriptor

    async def generate(self, request: ModelRequest) -> ModelResponse:
        """把通用模型请求交给已注入的异步函数。

        Args:
            request: 与供应商无关的模型请求。

        Returns:
            调用方函数返回的通用模型响应。

        Raises:
            RuntimeError: 客户端已关闭。
            TypeError: 回调返回值不是 :class:`ModelResponse`。
        """
        with self._lock:
            if self._closed:
                raise RuntimeError("callable model client is closed")
        response = await self._generate_callback(request)
        if not isinstance(response, ModelResponse):
            raise TypeError("generate callback must return ModelResponse")
        return response

    async def aclose(self) -> None:
        """最多一次调用已注入的异步关闭函数。"""
        with self._lock:
            if self._closed:
                return
            self._closed = True
            callback = self._close_callback
        if callback is not None:
            await callback()


__all__ = [
    "CallableModelClient",
    "ModelCloseCallback",
    "ModelGenerateCallback",
]
