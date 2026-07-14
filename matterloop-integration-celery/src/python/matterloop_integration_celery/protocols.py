"""定义 Celery 集成依赖的最小结构协议。"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Protocol, runtime_checkable

from matterloop_core import LoopRequest, LoopResult, ResumeMode

CeleryTaskFunction = Callable[..., Mapping[str, object]]


@runtime_checkable
class CeleryControl(Protocol):
    """Celery 控制面撤销能力的最小协议。"""

    def revoke(self, task_id: str, *, terminate: bool = False) -> object:
        """尽力阻止指定任务开始或继续排队。"""
        ...


@runtime_checkable
class CeleryApp(Protocol):
    """生产者和任务注册所需的 Celery 应用最小协议。"""

    @property
    def control(self) -> CeleryControl:
        """返回 Celery 控制面。"""
        ...

    def send_task(
        self,
        name: str,
        args: tuple[object, ...] | None = None,
        kwargs: Mapping[str, object] | None = None,
        **options: object,
    ) -> object:
        """按任务名称投递 JSON 消息。"""
        ...

    def task(self, **options: object) -> Callable[[CeleryTaskFunction], CeleryTaskFunction]:
        """注册一个 Worker 任务函数。"""
        ...


@runtime_checkable
class CeleryWorkerRuntime(Protocol):
    """Worker 执行启动和恢复命令所需的异步 Runtime 协议。"""

    async def run(self, request: LoopRequest, *, run_id: str | None = None) -> LoopResult:
        """以指定标识启动一次 Loop。"""
        ...

    async def resume(
        self,
        run_id: str,
        *,
        mode: ResumeMode = ResumeMode.CONTINUE,
    ) -> LoopResult:
        """按模式恢复已有 Loop。"""
        ...


@runtime_checkable
class AsyncCloser(Protocol):
    """工厂创建的可选异步资源关闭协议。"""

    async def aclose(self) -> None:
        """释放当前任务持有的连接或线程资源。"""
        ...
