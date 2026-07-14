"""Celery Worker 任务注册和 CAS 幂等执行测试。"""

from __future__ import annotations

import asyncio
import sys
from datetime import datetime, timedelta, timezone
from types import ModuleType

import pytest
from fakes import FakeCeleryApp, FakeCloser, FakeRunRepository, FakeWorkerRuntime
from matterloop_core import LoopRequest, ResumeMode
from matterloop_integration_celery import (
    RESUME_TASK_NAME,
    RUN_TASK_NAME,
    CeleryMessageCodec,
    CeleryWorkerDependencies,
    register_tasks,
)
from matterloop_runtime import RunRecord, RunStatus


def test_registered_start_task_claims_once_and_skips_duplicate() -> None:
    app = FakeCeleryApp()
    repository = FakeRunRepository()
    runtime = FakeWorkerRuntime()
    closer = FakeCloser()
    request = LoopRequest(goal="幂等任务")
    asyncio.run(repository.create(RunRecord(run_id="run-1", request=request)))
    module_name = "matterloop_test_celery_factory"
    module = ModuleType(module_name)

    def create_dependencies() -> CeleryWorkerDependencies:
        return CeleryWorkerDependencies(runtime, repository, closer)

    module.create_dependencies = create_dependencies  # type: ignore[attr-defined]
    registered = register_tasks(app, f"{module_name}:create_dependencies")
    # 注册时不会导入业务工厂；它只在 Worker 真正执行任务时解析。
    assert module_name not in sys.modules
    sys.modules[module_name] = module
    try:
        payload = CeleryMessageCodec().encode_request(request)
        first = registered.run(run_id="run-1", request=payload)
        duplicate = registered.run(run_id="run-1", request=payload)
    finally:
        del sys.modules[module_name]

    assert first == {
        "run_id": "run-1",
        "status": "completed",
        "version": 2,
        "duplicate": False,
    }
    assert duplicate["duplicate"] is True
    assert len(runtime.run_calls) == 1
    assert closer.calls == 2
    record = asyncio.run(repository.get("run-1"))
    assert record is not None
    assert record.status is RunStatus.COMPLETED
    assert record.result is not None
    assert app.tasks[RUN_TASK_NAME] is registered.run
    assert app.task_options[RUN_TASK_NAME]["acks_late"] is True
    assert app.task_options[RUN_TASK_NAME]["reject_on_worker_lost"] is True


def test_registered_resume_task_passes_only_mode_and_run_id() -> None:
    app = FakeCeleryApp()
    repository = FakeRunRepository()
    runtime = FakeWorkerRuntime()
    request = LoopRequest(goal="恢复任务")
    asyncio.run(
        repository.create(RunRecord(run_id="run-resume", request=request, status=RunStatus.QUEUED))
    )
    module_name = "matterloop_test_resume_factory"
    module = ModuleType(module_name)

    def create_dependencies() -> CeleryWorkerDependencies:
        return CeleryWorkerDependencies(runtime, repository)

    module.create_dependencies = create_dependencies  # type: ignore[attr-defined]
    sys.modules[module_name] = module
    try:
        registered = register_tasks(app, f"{module_name}:create_dependencies")
        result = registered.resume(run_id="run-resume", resume_mode="replan")
    finally:
        del sys.modules[module_name]

    assert result["status"] == "completed"
    assert runtime.resume_calls == [("run-resume", ResumeMode.REPLAN)]
    assert app.tasks[RESUME_TASK_NAME] is registered.resume


def test_worker_marks_failure_without_storing_supplier_error_text() -> None:
    app = FakeCeleryApp()
    repository = FakeRunRepository()
    runtime = FakeWorkerRuntime(fail=True)
    request = LoopRequest(goal="失败任务")
    asyncio.run(repository.create(RunRecord(run_id="run-fail", request=request)))
    module_name = "matterloop_test_failure_factory"
    module = ModuleType(module_name)

    def create_dependencies() -> CeleryWorkerDependencies:
        return CeleryWorkerDependencies(runtime, repository)

    module.create_dependencies = create_dependencies  # type: ignore[attr-defined]
    sys.modules[module_name] = module
    try:
        registered = register_tasks(app, f"{module_name}:create_dependencies")
        with pytest.raises(RuntimeError, match="sensitive supplier detail"):
            registered.run(
                run_id="run-fail",
                request=CeleryMessageCodec().encode_request(request),
            )
    finally:
        del sys.modules[module_name]

    record = asyncio.run(repository.get("run-fail"))
    assert record is not None
    assert record.status is RunStatus.FAILED
    assert "sensitive supplier detail" not in record.error


def test_active_running_claim_is_not_executed_twice() -> None:
    app = FakeCeleryApp()
    repository = FakeRunRepository()
    runtime = FakeWorkerRuntime()
    request = LoopRequest(goal="活跃任务")
    asyncio.run(
        repository.create(
            RunRecord(
                run_id="run-active",
                request=request,
                status=RunStatus.RUNNING,
                version=1,
                updated_at=datetime.now(timezone.utc),
            )
        )
    )
    module_name = "matterloop_test_active_claim_factory"
    module = ModuleType(module_name)

    def create_dependencies() -> CeleryWorkerDependencies:
        return CeleryWorkerDependencies(
            runtime,
            repository,
            claim_lease_seconds=60,
        )

    module.create_dependencies = create_dependencies  # type: ignore[attr-defined]
    sys.modules[module_name] = module
    try:
        registered = register_tasks(app, f"{module_name}:create_dependencies")
        result = registered.run(
            run_id="run-active",
            request=CeleryMessageCodec().encode_request(request),
        )
    finally:
        del sys.modules[module_name]

    assert result["duplicate"] is True
    assert runtime.run_calls == []


def test_stale_running_claim_can_be_recovered_by_redelivery() -> None:
    app = FakeCeleryApp()
    repository = FakeRunRepository()
    runtime = FakeWorkerRuntime()
    request = LoopRequest(goal="恢复陈旧任务")
    asyncio.run(
        repository.create(
            RunRecord(
                run_id="run-stale",
                request=request,
                status=RunStatus.RUNNING,
                version=1,
                updated_at=datetime.now(timezone.utc) - timedelta(minutes=5),
            )
        )
    )
    module_name = "matterloop_test_stale_claim_factory"
    module = ModuleType(module_name)

    def create_dependencies() -> CeleryWorkerDependencies:
        return CeleryWorkerDependencies(
            runtime,
            repository,
            claim_lease_seconds=60,
        )

    module.create_dependencies = create_dependencies  # type: ignore[attr-defined]
    sys.modules[module_name] = module
    try:
        registered = register_tasks(app, f"{module_name}:create_dependencies")
        result = registered.run(
            run_id="run-stale",
            request=CeleryMessageCodec().encode_request(request),
        )
    finally:
        del sys.modules[module_name]

    assert result == {
        "run_id": "run-stale",
        "status": "completed",
        "version": 3,
        "duplicate": False,
    }
    assert len(runtime.run_calls) == 1
