"""FastAPI 路由依赖的最小运行时结构协议。"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Protocol, runtime_checkable

from matterloop_core import LoopRequest, LoopResult, ResumeMode
from matterloop_runtime import RunRecord


@runtime_checkable
class DirectRuntimeProtocol(Protocol):
    """直接执行 Loop 的异步运行时协议。"""

    async def run(self, request: LoopRequest, *, run_id: str | None = None) -> LoopResult:
        """执行一次 Loop 并返回最终结果。"""
        ...

    async def resume(
        self,
        run_id: str,
        *,
        mode: ResumeMode = ResumeMode.CONTINUE,
    ) -> LoopResult:
        """恢复暂停或阻塞的 Loop。"""
        ...

    async def cancel(self, run_id: str) -> bool:
        """请求在下一个安全边界取消运行。"""
        ...


@runtime_checkable
class QueueRuntimeProtocol(Protocol):
    """提供持久查询与队列提交能力的异步运行时协议。"""

    async def submit(self, request: LoopRequest, *, run_id: str | None = None) -> str:
        """提交新运行并返回运行标识。"""
        ...

    async def get(self, run_id: str) -> RunRecord | None:
        """读取单个运行记录。"""
        ...

    async def list(self, *, limit: int = 100, offset: int = 0) -> tuple[RunRecord, ...]:
        """分页列出运行记录。"""
        ...

    async def cancel(self, run_id: str) -> bool:
        """请求取消队列运行。"""
        ...

    async def resume(
        self,
        run_id: str,
        *,
        mode: ResumeMode = ResumeMode.CONTINUE,
    ) -> bool:
        """把可恢复运行重新提交到队列。"""
        ...

    async def list_events(
        self,
        run_id: str,
        *,
        after: str | None = None,
        limit: int = 100,
    ) -> tuple[Mapping[str, object], ...]:
        """分页读取运行审计事件。"""
        ...


RuntimeProtocol = DirectRuntimeProtocol | QueueRuntimeProtocol

__all__ = ["DirectRuntimeProtocol", "QueueRuntimeProtocol", "RuntimeProtocol"]
