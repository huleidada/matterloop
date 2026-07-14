"""多 Agent 团队控制器的异步与同步调用门面。"""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Coroutine, Iterable
from concurrent.futures import Future
from typing import Protocol, TypeVar, runtime_checkable

from matterloop_core import HumanResponse

from matterloop_agents.collaboration.errors import TeamRuntimeClosedError
from matterloop_agents.collaboration.models import (
    TeamRequest,
    TeamResult,
    TeamSnapshot,
)
from matterloop_agents.collaboration.orchestrator import TeamOrchestrator

T = TypeVar("T")


@runtime_checkable
class AsyncTeamResource(Protocol):
    """由异步团队运行时统一关闭的资源。"""

    async def aclose(self) -> None:
        """异步释放资源。"""
        ...


class AsyncTeamRuntime:
    """面向异步应用的标准团队协作门面。

    Args:
        orchestrator: 已由用户显式装配的团队控制器。
        resources: 运行时关闭时按注册逆序释放的可选资源。
    """

    def __init__(
        self,
        orchestrator: TeamOrchestrator,
        *,
        resources: Iterable[AsyncTeamResource] = (),
    ) -> None:
        self._orchestrator = orchestrator
        self._resources = tuple(resources)
        self._closed = False

    def create_run_id(self) -> str:
        """创建新的团队运行标识。"""
        self._ensure_open()
        return self._orchestrator.create_run_id()

    async def run(
        self,
        request: TeamRequest,
        *,
        run_id: str | None = None,
    ) -> TeamResult:
        """异步启动一次团队运行。

        Args:
            request: 团队目标和执行边界。
            run_id: 可选的预生成运行标识。

        Returns:
            最新团队运行结果。
        """
        self._ensure_open()
        return await self._orchestrator.run(request, run_id=run_id)

    async def resume(self, run_id: str) -> TeamResult:
        """异步恢复一次暂停或阻塞运行。

        Args:
            run_id: 团队运行标识。

        Returns:
            恢复后的最新结果。
        """
        self._ensure_open()
        return await self._orchestrator.resume(run_id)

    async def submit_human_response(
        self,
        run_id: str,
        response: HumanResponse,
    ) -> TeamResult:
        """异步提交人工反馈，由调用方随后显式恢复运行。

        Args:
            run_id: 团队运行标识。
            response: 与当前待处理交互匹配的结构化响应。

        Returns:
            提交后仍暂停或已阻塞的最新结果。
        """
        self._ensure_open()
        return await self._orchestrator.submit_human_response(run_id, response)

    async def cancel(self, run_id: str) -> bool:
        """请求取消团队运行。

        Args:
            run_id: 团队运行标识。

        Returns:
            控制器是否接受新的取消请求。
        """
        self._ensure_open()
        return await self._orchestrator.cancel(run_id)

    async def get(self, run_id: str) -> TeamResult:
        """读取团队运行结果。

        Args:
            run_id: 团队运行标识。

        Returns:
            最新持久化结果。
        """
        self._ensure_open()
        return await self._orchestrator.get(run_id)

    async def list(self) -> tuple[TeamSnapshot, ...]:
        """列出全部团队运行快照。"""
        self._ensure_open()
        return await self._orchestrator.list()

    async def aclose(self) -> None:
        """按注册逆序关闭资源；可重复调用。"""
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

    async def __aenter__(self) -> AsyncTeamRuntime:
        """返回当前异步团队运行时。"""
        self._ensure_open()
        return self

    async def __aexit__(self, exc_type: object, exc: object, traceback: object) -> None:
        """离开异步上下文时关闭资源。"""
        await self.aclose()

    def _ensure_open(self) -> None:
        if self._closed:
            raise TeamRuntimeClosedError("async team runtime is closed")


class LocalTeamRuntime:
    """在专用事件循环线程上提供阻塞式团队调用接口。

    Args:
        runtime: 需要转换为同步接口的异步团队运行时。
        thread_name: 后台事件循环线程名称。
    """

    def __init__(
        self,
        runtime: AsyncTeamRuntime,
        *,
        thread_name: str = "matterloop-team-runtime",
    ) -> None:
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
        """创建新的团队运行标识。"""
        self._ensure_open()
        return self._runtime.create_run_id()

    def run(
        self,
        request: TeamRequest,
        *,
        run_id: str | None = None,
    ) -> TeamResult:
        """阻塞当前线程直到团队运行停止。

        Args:
            request: 团队目标和执行边界。
            run_id: 可选的预生成运行标识。

        Returns:
            最新团队结果。
        """
        return self._submit(self._runtime.run(request, run_id=run_id))

    def resume(self, run_id: str) -> TeamResult:
        """阻塞当前线程直到恢复运行停止。

        Args:
            run_id: 团队运行标识。

        Returns:
            恢复后的最新结果。
        """
        return self._submit(self._runtime.resume(run_id))

    def submit_human_response(
        self,
        run_id: str,
        response: HumanResponse,
    ) -> TeamResult:
        """阻塞提交人工反馈，不隐式恢复团队运行。

        Args:
            run_id: 团队运行标识。
            response: 与当前待处理交互匹配的结构化响应。

        Returns:
            提交后的最新团队结果。
        """
        return self._submit(self._runtime.submit_human_response(run_id, response))

    def cancel(self, run_id: str) -> bool:
        """请求取消团队运行。

        Args:
            run_id: 团队运行标识。

        Returns:
            控制器是否接受新的取消请求。
        """
        return self._submit(self._runtime.cancel(run_id))

    def get(self, run_id: str) -> TeamResult:
        """阻塞读取最新团队结果。

        Args:
            run_id: 团队运行标识。

        Returns:
            最新持久化结果。
        """
        return self._submit(self._runtime.get(run_id))

    def list(self) -> tuple[TeamSnapshot, ...]:
        """阻塞列出全部团队快照。"""
        return self._submit(self._runtime.list())

    def close(self) -> None:
        """关闭异步资源、后台事件循环和线程；可重复调用。"""
        with self._state_lock:
            if self._closed:
                return
            self._closed = True
        if threading.current_thread() is self._thread:
            task = self._loop.create_task(self._runtime.aclose())
            task.add_done_callback(lambda _: self._loop.stop())
            return
        future = asyncio.run_coroutine_threadsafe(self._runtime.aclose(), self._loop)
        try:
            future.result()
        finally:
            self._loop.call_soon_threadsafe(self._loop.stop)
            self._thread.join()

    def __enter__(self) -> LocalTeamRuntime:
        """返回当前同步团队运行时。"""
        self._ensure_open()
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        """离开同步上下文时释放后台线程。"""
        self.close()

    def _submit(self, coroutine: Coroutine[object, object, T]) -> T:
        if threading.current_thread() is self._thread:
            coroutine.close()
            raise RuntimeError("cannot block the LocalTeamRuntime event-loop thread")
        with self._state_lock:
            if self._closed:
                coroutine.close()
                raise TeamRuntimeClosedError("local team runtime is closed")
            try:
                future: Future[T] = asyncio.run_coroutine_threadsafe(
                    coroutine,
                    self._loop,
                )
            except Exception:
                coroutine.close()
                raise
        return future.result()

    def _ensure_open(self) -> None:
        with self._state_lock:
            if self._closed:
                raise TeamRuntimeClosedError("local team runtime is closed")

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


__all__ = ["AsyncTeamResource", "AsyncTeamRuntime", "LocalTeamRuntime"]
