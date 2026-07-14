"""Celery 集成测试使用的无外部服务假实现。"""

from __future__ import annotations

from collections.abc import Callable, Mapping

from matterloop_core import (
    LoopRequest,
    LoopResult,
    LoopStatus,
    ResumeMode,
    StopReason,
)
from matterloop_runtime import DuplicateRunError, RunRecord


class FakeControl:
    """记录 Celery 撤销请求。"""

    def __init__(self) -> None:
        self.revoked: list[tuple[str, bool]] = []

    def revoke(self, task_id: str, *, terminate: bool = False) -> object:
        """记录撤销参数。"""
        self.revoked.append((task_id, terminate))
        return None


class FakeCeleryApp:
    """记录任务注册与投递，而不连接 Broker。"""

    def __init__(self) -> None:
        self.control = FakeControl()
        self.tasks: dict[str, Callable[..., Mapping[str, object]]] = {}
        self.task_options: dict[str, Mapping[str, object]] = {}
        self.sent: list[tuple[str, Mapping[str, object] | None, Mapping[str, object]]] = []

    def send_task(
        self,
        name: str,
        args: tuple[object, ...] | None = None,
        kwargs: Mapping[str, object] | None = None,
        **options: object,
    ) -> object:
        """记录一条待发送任务。"""
        assert args is None
        self.sent.append((name, kwargs, options))
        return object()

    def task(
        self,
        **options: object,
    ) -> Callable[
        [Callable[..., Mapping[str, object]]],
        Callable[..., Mapping[str, object]],
    ]:
        """模拟 Celery 任务装饰器。"""

        def decorator(
            function: Callable[..., Mapping[str, object]],
        ) -> Callable[..., Mapping[str, object]]:
            name = options.get("name")
            assert isinstance(name, str)
            self.tasks[name] = function
            self.task_options[name] = dict(options)
            return function

        return decorator


class FakeRunRepository:
    """提供确定性 CAS 的进程内测试仓储。"""

    def __init__(self) -> None:
        self.records: dict[str, RunRecord] = {}

    async def create(self, record: RunRecord) -> None:
        """创建测试运行记录。"""
        if record.run_id in self.records:
            raise DuplicateRunError(record.run_id)
        self.records[record.run_id] = record

    async def get(self, run_id: str) -> RunRecord | None:
        """读取测试运行记录。"""
        return self.records.get(run_id)

    async def list(self, *, limit: int = 100, offset: int = 0) -> tuple[RunRecord, ...]:
        """按插入顺序返回测试记录。"""
        return tuple(self.records.values())[offset : offset + limit]

    async def compare_and_set(
        self,
        run_id: str,
        expected_version: int,
        replacement: RunRecord,
    ) -> bool:
        """仅在版本匹配时替换测试记录。"""
        current = self.records.get(run_id)
        if current is None or current.version != expected_version:
            return False
        self.records[run_id] = replacement
        return True


class FakeWorkerRuntime:
    """记录 Worker 调用并返回可配置结果。"""

    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.run_calls: list[tuple[LoopRequest, str | None]] = []
        self.resume_calls: list[tuple[str, ResumeMode]] = []

    async def run(self, request: LoopRequest, *, run_id: str | None = None) -> LoopResult:
        """记录启动调用并返回完成结果。"""
        self.run_calls.append((request, run_id))
        if self.fail:
            raise RuntimeError("sensitive supplier detail")
        assert run_id is not None
        return completed_result(run_id)

    async def resume(
        self,
        run_id: str,
        *,
        mode: ResumeMode = ResumeMode.CONTINUE,
    ) -> LoopResult:
        """记录恢复调用并返回完成结果。"""
        self.resume_calls.append((run_id, mode))
        if self.fail:
            raise RuntimeError("sensitive supplier detail")
        return completed_result(run_id)


class FakeCloser:
    """记录工厂资源关闭次数。"""

    def __init__(self) -> None:
        self.calls = 0

    async def aclose(self) -> None:
        """记录一次关闭。"""
        self.calls += 1


def completed_result(run_id: str) -> LoopResult:
    """创建最小完成结果。"""
    return LoopResult(
        run_id=run_id,
        status=LoopStatus.COMPLETED,
        output="done",
        cycles=1,
        total_attempts=1,
        completed_steps=1,
        records=(),
        stop_reason=StopReason.COMPLETED,
    )
