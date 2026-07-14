"""同步与异步 Loop 运行门面。"""

from __future__ import annotations

import asyncio
import inspect
import threading
from collections.abc import Coroutine, Iterable
from concurrent.futures import Future
from typing import Protocol, TypeVar

from matterloop_core import HumanResponse, LoopRequest, LoopResult, ResumeMode

from matterloop_runtime.errors import RuntimeClosedError

T = TypeVar("T")


class LoopEngine(Protocol):
    """运行门面所需的最小 Loop 内核接口。"""

    async def run(self, request: LoopRequest, *, run_id: str | None = None) -> LoopResult:
        """启动一次新运行。"""
        ...

    async def resume(
        self,
        run_id: str,
        *,
        mode: ResumeMode = ResumeMode.CONTINUE,
    ) -> LoopResult:
        """恢复一次暂停或阻塞的运行。"""
        ...

    async def submit_human_response(
        self,
        run_id: str,
        response: HumanResponse,
    ) -> LoopResult:
        """持久化人工反馈，但不隐式恢复运行。"""
        ...

    def cancel(self, run_id: str) -> bool:
        """请求在安全边界取消运行。"""
        ...

    def create_run_id(self) -> str:
        """创建全局唯一的运行标识。"""
        ...


class AsyncClosable(Protocol):
    """由异步运行门面统一关闭的资源协议。"""

    async def aclose(self) -> None:
        """异步释放资源。"""
        ...


class AsyncRuntime:
    """面向异步应用的标准 Loop 运行门面。

    Args:
        engine: 满足最小运行协议的 Loop 内核。
        resources: 由运行门面按注册逆序关闭的资源。
    """

    def __init__(
        self,
        engine: LoopEngine,
        *,
        resources: Iterable[AsyncClosable] = (),
    ) -> None:
        self._engine = engine
        self._resources = tuple(resources)
        self._closed = False

    def create_run_id(self) -> str:
        """预先创建运行标识，便于调用方并发取消。

        Returns:
            新的运行标识。
        """
        self._ensure_open()
        return self._engine.create_run_id()

    async def run(self, request: LoopRequest, *, run_id: str | None = None) -> LoopResult:
        """异步启动一次 Loop。

        Args:
            request: Loop 请求。
            run_id: 可选的预生成运行标识。

        Returns:
            Loop 运行结果。
        """
        self._ensure_open()
        return await self._engine.run(request, run_id=run_id)

    async def resume(
        self,
        run_id: str,
        *,
        mode: ResumeMode = ResumeMode.CONTINUE,
    ) -> LoopResult:
        """异步恢复一次 Loop。

        Args:
            run_id: 需要恢复的运行标识。
            mode: 精确继续或重新规划。

        Returns:
            恢复后的运行结果。
        """
        self._ensure_open()
        return await self._engine.resume(run_id, mode=mode)

    async def submit_human_response(
        self,
        run_id: str,
        response: HumanResponse,
    ) -> LoopResult:
        """异步提交一次结构化人工反馈。

        Args:
            run_id: 正在等待人工响应的运行标识。
            response: 与当前待处理交互匹配的响应。

        Returns:
            反馈已持久化但仍未自动恢复的 Loop 结果。
        """
        self._ensure_open()
        return await self._engine.submit_human_response(run_id, response)

    async def cancel(self, run_id: str) -> bool:
        """请求取消一次 Loop。

        Args:
            run_id: 目标运行标识。

        Returns:
            内核是否接受取消请求。
        """
        self._ensure_open()
        result = self._engine.cancel(run_id)
        if inspect.isawaitable(result):
            return bool(await result)
        return result

    async def aclose(self) -> None:
        """按注册逆序关闭装配到运行时的资源；可重复调用。"""
        if self._closed:
            return
        self._closed = True
        first_error: Exception | None = None
        for resource in reversed(self._resources):
            try:
                await resource.aclose()
            except Exception as exc:
                if first_error is None:
                    first_error = exc
        if first_error is not None:
            raise first_error

    async def __aenter__(self) -> AsyncRuntime:
        """返回当前异步运行时。"""
        self._ensure_open()
        return self

    async def __aexit__(self, exc_type: object, exc: object, traceback: object) -> None:
        """离开异步上下文时关闭资源。"""
        await self.aclose()

    def _ensure_open(self) -> None:
        if self._closed:
            raise RuntimeClosedError("async runtime is closed")


class LocalRuntime:
    """在专用事件循环线程上提供阻塞式运行接口。

    事件循环与调用线程隔离，因此即使调用方本身处在异步框架线程中，也不会调用
    ``asyncio.run`` 或嵌套当前事件循环。

    Args:
        runtime: 需要转换为同步接口的异步运行门面。
        thread_name: 后台事件循环线程名称。
    """

    def __init__(self, runtime: AsyncRuntime, *, thread_name: str = "matterloop-runtime") -> None:
        self._runtime = runtime
        self._loop = asyncio.new_event_loop()
        self._ready = threading.Event()
        self._closed = False
        self._state_lock = threading.Lock()
        self._thread = threading.Thread(
            target=self._serve,
            name=thread_name,
            daemon=True,
        )
        self._thread.start()
        self._ready.wait()

    def create_run_id(self) -> str:
        """创建运行标识。

        Returns:
            新的运行标识。
        """
        self._ensure_open()
        return self._runtime.create_run_id()

    def run(self, request: LoopRequest, *, run_id: str | None = None) -> LoopResult:
        """阻塞当前线程直到 Loop 结束。

        Args:
            request: Loop 请求。
            run_id: 可选的预生成运行标识。

        Returns:
            Loop 运行结果。
        """
        return self._submit(self._runtime.run(request, run_id=run_id))

    def resume(
        self,
        run_id: str,
        *,
        mode: ResumeMode = ResumeMode.CONTINUE,
    ) -> LoopResult:
        """阻塞当前线程直到恢复运行结束。

        Args:
            run_id: 需要恢复的运行标识。
            mode: 精确继续或重新规划。

        Returns:
            Loop 运行结果。
        """
        return self._submit(self._runtime.resume(run_id, mode=mode))

    def submit_human_response(
        self,
        run_id: str,
        response: HumanResponse,
    ) -> LoopResult:
        """在专用事件循环中阻塞提交人工反馈。

        Args:
            run_id: 正在等待人工响应的运行标识。
            response: 与当前待处理交互匹配的响应。

        Returns:
            反馈已持久化的 Loop 结果。
        """
        return self._submit(self._runtime.submit_human_response(run_id, response))

    def cancel(self, run_id: str) -> bool:
        """请求取消运行。

        Args:
            run_id: 目标运行标识。

        Returns:
            内核是否接受取消请求。
        """
        return self._submit(self._runtime.cancel(run_id))

    def close(self) -> None:
        """停止后台事件循环并释放线程；可重复调用。"""
        with self._state_lock:
            if self._closed:
                return
            self._closed = True
        if threading.current_thread() is self._thread:
            task = self._loop.create_task(self._runtime.aclose())
            task.add_done_callback(lambda _: self._loop.stop())
            return
        close_future = asyncio.run_coroutine_threadsafe(self._runtime.aclose(), self._loop)
        try:
            close_future.result()
        finally:
            self._loop.call_soon_threadsafe(self._loop.stop)
            self._thread.join()

    def __enter__(self) -> LocalRuntime:
        """返回当前同步运行时。"""
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        """离开上下文时关闭后台事件循环。"""
        self.close()

    def _submit(self, coroutine: Coroutine[object, object, T]) -> T:
        try:
            self._ensure_open()
        except Exception:
            coroutine.close()
            raise
        if threading.current_thread() is self._thread:
            coroutine.close()
            raise RuntimeError("cannot block the LocalRuntime event-loop thread")
        future: Future[T] = asyncio.run_coroutine_threadsafe(coroutine, self._loop)
        return future.result()

    def _ensure_open(self) -> None:
        with self._state_lock:
            if self._closed:
                raise RuntimeClosedError("local runtime is closed")

    def _serve(self) -> None:
        asyncio.set_event_loop(self._loop)
        self._ready.set()
        self._loop.run_forever()
        pending = asyncio.all_tasks(self._loop)
        for task in pending:
            task.cancel()
        if pending:
            self._loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
        self._loop.run_until_complete(self._loop.shutdown_asyncgens())
        self._loop.close()
