"""注册 Celery Worker 任务并通过 CAS 提供幂等执行。"""

from __future__ import annotations

import asyncio
import importlib
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from typing import cast

from matterloop_core import LoopRequest, LoopResult, LoopStatus, ResumeMode
from matterloop_runtime import RunNotFoundError, RunRecord, RunRepository, RunStatus

from matterloop_integration_celery.codec import CeleryMessageCodec
from matterloop_integration_celery.errors import (
    CeleryFactoryError,
    CeleryRunConflictError,
    CeleryWorkerError,
)
from matterloop_integration_celery.producer import RESUME_TASK_NAME, RUN_TASK_NAME
from matterloop_integration_celery.protocols import (
    AsyncCloser,
    CeleryApp,
    CeleryTaskFunction,
    CeleryWorkerRuntime,
)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True, slots=True)
class CeleryWorkerDependencies:
    """Worker 工厂为单次任务创建的运行依赖。

    Args:
        runtime: 实际执行启动或恢复命令的异步 Runtime。
        repository: 与 API 进程共享、支持 CAS 的运行仓储。
        closer: 可选的异步资源关闭器，例如持有连接池的客户端。
        claim_lease_seconds: `RUNNING` 认领在此时间内视为活跃；超过后允许重投任务
            通过 CAS 接管。该值应大于单次任务正常执行时长。
    """

    runtime: CeleryWorkerRuntime
    repository: RunRepository
    closer: AsyncCloser | None = None
    claim_lease_seconds: float = 3600.0

    def __post_init__(self) -> None:
        """在任务开始前验证工厂依赖满足结构协议。

        Raises:
            TypeError: Runtime、仓储或关闭器不满足对应协议。
        """
        if not isinstance(self.runtime, CeleryWorkerRuntime):
            raise TypeError("runtime must implement CeleryWorkerRuntime")
        if not isinstance(self.repository, RunRepository):
            raise TypeError("repository must implement RunRepository")
        if self.closer is not None and not isinstance(self.closer, AsyncCloser):
            raise TypeError("closer must implement AsyncCloser")
        if self.claim_lease_seconds <= 0:
            raise ValueError("claim_lease_seconds must be greater than 0")


@dataclass(frozen=True, slots=True)
class RegisteredCeleryTasks:
    """返回已注册任务，便于测试和 Worker 启动诊断。"""

    run: CeleryTaskFunction
    resume: CeleryTaskFunction


class _TaskProcessor:
    def __init__(self, runtime_factory_path: str, codec: CeleryMessageCodec) -> None:
        self._runtime_factory_path = runtime_factory_path
        self._codec = codec

    async def run(self, run_id: str, request_payload: Mapping[str, object]) -> dict[str, object]:
        request = self._codec.decode_request(request_payload)
        dependencies = self._create_dependencies()
        try:
            claimed = await self._claim(
                dependencies.repository,
                run_id,
                claim_lease_seconds=dependencies.claim_lease_seconds,
                request=request,
            )
            if claimed is None:
                return await self._duplicate_result(dependencies.repository, run_id)
            try:
                result = await dependencies.runtime.run(request, run_id=run_id)
            except Exception as exc:
                await self._mark_failed(dependencies.repository, claimed, exc)
                raise
            return await self._complete(dependencies.repository, claimed, result)
        finally:
            if dependencies.closer is not None:
                await dependencies.closer.aclose()

    async def resume(self, run_id: str, mode_value: str) -> dict[str, object]:
        try:
            mode = ResumeMode(mode_value)
        except ValueError as exc:
            raise CeleryWorkerError(f"unsupported resume mode: {mode_value}") from exc
        dependencies = self._create_dependencies()
        try:
            claimed = await self._claim(
                dependencies.repository,
                run_id,
                claim_lease_seconds=dependencies.claim_lease_seconds,
            )
            if claimed is None:
                return await self._duplicate_result(dependencies.repository, run_id)
            try:
                result = await dependencies.runtime.resume(run_id, mode=mode)
            except Exception as exc:
                await self._mark_failed(dependencies.repository, claimed, exc)
                raise
            return await self._complete(dependencies.repository, claimed, result)
        finally:
            if dependencies.closer is not None:
                await dependencies.closer.aclose()

    def _create_dependencies(self) -> CeleryWorkerDependencies:
        factory = _resolve_factory(self._runtime_factory_path)
        try:
            dependencies = factory()
        except Exception as exc:
            raise CeleryFactoryError(f"runtime factory failed ({type(exc).__name__})") from exc
        if not isinstance(dependencies, CeleryWorkerDependencies):
            raise CeleryFactoryError("runtime factory must return CeleryWorkerDependencies")
        return dependencies

    async def _claim(
        self,
        repository: RunRepository,
        run_id: str,
        *,
        claim_lease_seconds: float,
        request: LoopRequest | None = None,
    ) -> RunRecord | None:
        record = await repository.get(run_id)
        if record is None:
            raise RunNotFoundError(run_id)
        if request is not None and self._codec.encode_request(
            record.request
        ) != self._codec.encode_request(request):
            raise CeleryRunConflictError("queued request does not match run repository")
        now = _utc_now()
        claimable = record.status is RunStatus.QUEUED
        if record.status is RunStatus.RUNNING:
            if record.updated_at.tzinfo is None:
                raise CeleryWorkerError("run updated_at must include a timezone")
            claimable = record.updated_at + timedelta(seconds=claim_lease_seconds) <= now
        if not claimable:
            return None
        claimed = replace(
            record,
            status=RunStatus.RUNNING,
            version=record.version + 1,
            error="",
            updated_at=now,
        )
        if not await repository.compare_and_set(run_id, record.version, claimed):
            return None
        return claimed

    @staticmethod
    async def _complete(
        repository: RunRepository,
        claimed: RunRecord,
        result: LoopResult,
    ) -> dict[str, object]:
        if result.run_id != claimed.run_id:
            error = CeleryWorkerError("runtime result run_id does not match queued run")
            await _TaskProcessor._mark_failed(repository, claimed, error)
            raise error
        status = _run_status(result.status)
        completed = replace(
            claimed,
            status=status,
            version=claimed.version + 1,
            result=result,
            error=result.error,
            updated_at=_utc_now(),
        )
        updated = await repository.compare_and_set(claimed.run_id, claimed.version, completed)
        if not updated:
            current = await repository.get(claimed.run_id)
            if current is None:
                raise RunNotFoundError(claimed.run_id)
            return _task_result(current, duplicate=True)
        return _task_result(completed, duplicate=False)

    @staticmethod
    async def _mark_failed(
        repository: RunRepository,
        claimed: RunRecord,
        error: Exception,
    ) -> None:
        failed = replace(
            claimed,
            status=RunStatus.FAILED,
            version=claimed.version + 1,
            error=f"{type(error).__name__}: worker execution failed",
            updated_at=_utc_now(),
        )
        await repository.compare_and_set(claimed.run_id, claimed.version, failed)

    @staticmethod
    async def _duplicate_result(
        repository: RunRepository,
        run_id: str,
    ) -> dict[str, object]:
        current = await repository.get(run_id)
        if current is None:
            raise RunNotFoundError(run_id)
        return _task_result(current, duplicate=True)


def register_tasks(
    celery_app: CeleryApp,
    runtime_factory_path: str,
) -> RegisteredCeleryTasks:
    """注册启动与恢复任务，Runtime 只在 Worker 执行时通过导入路径创建。

    Args:
        celery_app: Celery 应用或满足最小协议的兼容对象。
        runtime_factory_path: `模块:工厂` 格式的导入路径。工厂必须无参数并返回
            `CeleryWorkerDependencies`。

    Returns:
        已注册的两个任务函数。

    Raises:
        ValueError: 工厂路径格式无效。
    """
    _validate_factory_path(runtime_factory_path)
    processor = _TaskProcessor(runtime_factory_path, CeleryMessageCodec())

    @celery_app.task(
        name=RUN_TASK_NAME,
        acks_late=True,
        reject_on_worker_lost=True,
        serializer="json",
    )
    def run_task(*, run_id: str, request: Mapping[str, object]) -> Mapping[str, object]:
        """执行只携带运行标识和请求 DTO 的启动消息。"""
        return asyncio.run(processor.run(run_id, request))

    @celery_app.task(
        name=RESUME_TASK_NAME,
        acks_late=True,
        reject_on_worker_lost=True,
        serializer="json",
    )
    def resume_task(*, run_id: str, resume_mode: str) -> Mapping[str, object]:
        """执行只携带运行标识和恢复模式的恢复消息。"""
        return asyncio.run(processor.resume(run_id, resume_mode))

    return RegisteredCeleryTasks(run=run_task, resume=resume_task)


def _validate_factory_path(path: str) -> None:
    module_name, separator, attribute_name = path.partition(":")
    if (
        separator != ":"
        or not module_name.strip()
        or not attribute_name.strip()
        or ":" in attribute_name
    ):
        raise ValueError("runtime_factory_path must use 'module:factory' format")


def _resolve_factory(path: str) -> Callable[[], CeleryWorkerDependencies]:
    _validate_factory_path(path)
    module_name, _, attribute_name = path.partition(":")
    try:
        value: object = importlib.import_module(module_name)
        for part in attribute_name.split("."):
            value = getattr(value, part)
    except (ImportError, AttributeError) as exc:
        raise CeleryFactoryError(f"cannot resolve runtime factory: {path}") from exc
    if not callable(value):
        raise CeleryFactoryError(f"runtime factory is not callable: {path}")
    return cast(Callable[[], CeleryWorkerDependencies], value)


def _run_status(status: LoopStatus) -> RunStatus:
    mapping = {
        LoopStatus.PAUSED: RunStatus.PAUSED,
        LoopStatus.BLOCKED: RunStatus.BLOCKED,
        LoopStatus.COMPLETED: RunStatus.COMPLETED,
        LoopStatus.FAILED: RunStatus.FAILED,
        LoopStatus.CANCELLED: RunStatus.CANCELLED,
        LoopStatus.TIMED_OUT: RunStatus.TIMED_OUT,
    }
    try:
        return mapping[status]
    except KeyError as exc:
        raise CeleryWorkerError(f"runtime returned unsettled status: {status.value}") from exc


def _task_result(record: RunRecord, *, duplicate: bool) -> dict[str, object]:
    return {
        "run_id": record.run_id,
        "status": record.status.value,
        "version": record.version,
        "duplicate": duplicate,
    }
