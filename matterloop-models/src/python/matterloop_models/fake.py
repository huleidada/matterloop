"""提供不连接任何外部服务的可编程假模型。"""

from __future__ import annotations

import asyncio
import inspect
from collections import deque
from collections.abc import Awaitable, Callable, Iterable

from matterloop_models.base import ModelRequest, ModelResponse
from matterloop_models.errors import FakeModelExhaustedError

ModelResponder = Callable[[ModelRequest], ModelResponse | Awaitable[ModelResponse]]


class FakeModelClient:
    """按队列或回调产生确定性响应，并保存请求以供断言。

    Args:
        responses: 依次返回的预设响应。
        responder: 根据请求动态生成响应的同步或异步回调。

    Raises:
        ValueError: 同时配置预设响应和回调。
    """

    def __init__(
        self,
        responses: Iterable[ModelResponse] = (),
        *,
        responder: ModelResponder | None = None,
    ) -> None:
        response_queue = deque(responses)
        if response_queue and responder is not None:
            raise ValueError("responses and responder are mutually exclusive")
        self._responses = response_queue
        self._responder = responder
        self._requests: list[ModelRequest] = []
        self._lock = asyncio.Lock()

    @property
    def requests(self) -> tuple[ModelRequest, ...]:
        """返回已接收请求的不可变快照。"""
        return tuple(self._requests)

    async def generate(self, request: ModelRequest) -> ModelResponse:
        """记录请求并返回下一条响应。

        Raises:
            FakeModelExhaustedError: 未配置回调且预设响应已经耗尽。
        """
        async with self._lock:
            self._requests.append(request)
            responder = self._responder
            if responder is None:
                if not self._responses:
                    raise FakeModelExhaustedError("fake model response queue is exhausted")
                return self._responses.popleft()

        response = responder(request)
        if inspect.isawaitable(response):
            return await response
        return response

    async def push(self, response: ModelResponse) -> None:
        """向响应队列末尾追加一条响应。

        Args:
            response: 后续 `generate` 调用需要消费的响应。
        """
        async with self._lock:
            self._responses.append(response)
