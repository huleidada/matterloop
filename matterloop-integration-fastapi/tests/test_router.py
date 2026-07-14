"""FastAPI 路由的鉴权、校验、分流与错误映射测试。"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace
from datetime import datetime, timezone
from typing import Annotated

from fastapi import FastAPI, Header, HTTPException
from fastapi.testclient import TestClient
from matterloop_core import (
    LoopRequest,
    LoopResult,
    LoopStatus,
    ResumeMode,
    StopReason,
)
from matterloop_integration_fastapi import (
    DirectRuntimeProtocol,
    QueueRuntimeProtocol,
    RuntimeProtocol,
    create_router,
)
from matterloop_runtime import (
    AsyncRuntime,
    InMemoryQueueBackend,
    InMemoryRunRepository,
    QueueRuntime,
    RunNotResumableError,
    RunRecord,
    RunStatus,
)


def _result(run_id: str, *, status: LoopStatus = LoopStatus.COMPLETED) -> LoopResult:
    """创建无需执行真实 Agent 的稳定测试结果。"""
    return LoopResult(
        run_id=run_id,
        status=status,
        output="done",
        cycles=1,
        total_attempts=1,
        completed_steps=1,
        records=(),
        stop_reason=StopReason.COMPLETED if status is LoopStatus.COMPLETED else None,
    )


class FakeQueueRuntime:
    """实现完整队列结构协议的测试替身。"""

    def __init__(self) -> None:
        self.records: dict[str, RunRecord] = {}
        self.events: dict[str, tuple[Mapping[str, object], ...]] = {}
        self.last_request: LoopRequest | None = None

    async def submit(self, request: LoopRequest, *, run_id: str | None = None) -> str:
        """创建排队记录。"""
        actual_run_id = run_id or "generated-run"
        self.last_request = request
        self.records[actual_run_id] = RunRecord(actual_run_id, request)
        return actual_run_id

    async def get(self, run_id: str) -> RunRecord | None:
        """读取记录。"""
        return self.records.get(run_id)

    async def list(self, *, limit: int = 100, offset: int = 0) -> tuple[RunRecord, ...]:
        """按插入顺序分页。"""
        return tuple(self.records.values())[offset : offset + limit]

    async def cancel(self, run_id: str) -> bool:
        """把未稳定运行标记为已取消。"""
        record = self.records.get(run_id)
        if record is None or record.status.is_settled:
            return False
        self.records[run_id] = replace(
            record,
            status=RunStatus.CANCELLED,
            version=record.version + 1,
        )
        return True

    async def resume(
        self,
        run_id: str,
        *,
        mode: ResumeMode = ResumeMode.CONTINUE,
    ) -> bool:
        """恢复暂停或阻塞记录。"""
        del mode
        record = self.records.get(run_id)
        if record is None or record.status not in {RunStatus.PAUSED, RunStatus.BLOCKED}:
            raise RunNotResumableError("run is not resumable")
        self.records[run_id] = replace(
            record,
            status=RunStatus.QUEUED,
            version=record.version + 1,
        )
        return True

    async def list_events(
        self,
        run_id: str,
        *,
        after: str | None = None,
        limit: int = 100,
    ) -> tuple[Mapping[str, object], ...]:
        """返回指定运行的事件切片。"""
        del after
        return self.events.get(run_id, ())[:limit]


class FakeDirectRuntime:
    """实现无仓储直接运行结构协议的测试替身。"""

    async def run(self, request: LoopRequest, *, run_id: str | None = None) -> LoopResult:
        """立即返回完成结果。"""
        del request
        return _result(run_id or "direct-run")

    async def resume(
        self,
        run_id: str,
        *,
        mode: ResumeMode = ResumeMode.CONTINUE,
    ) -> LoopResult:
        """立即返回恢复后的完成结果。"""
        del mode
        return _result(run_id)

    async def cancel(self, run_id: str) -> bool:
        """接受非空运行标识的取消请求。"""
        return bool(run_id)


async def _authenticate(
    x_api_key: Annotated[str | None, Header()] = None,
) -> str:
    """验证测试请求的 API Key。"""
    if x_api_key != "secret":
        raise HTTPException(status_code=401, detail="unauthorized")
    return x_api_key


def _client(runtime: RuntimeProtocol, *, prefix: str = "/loops") -> TestClient:
    """创建挂载待测路由的应用客户端。"""
    app = FastAPI()
    app.include_router(create_router(runtime, _authenticate, prefix=prefix))
    return TestClient(app)


def test_authentication_and_request_validation_run_before_runtime() -> None:
    """所有请求必须先通过鉴权和 Pydantic 边界校验。"""
    runtime = FakeQueueRuntime()
    client = _client(runtime)

    assert client.post("/loops/create", json={"goal": "work"}).status_code == 401
    invalid = client.post(
        "/loops/create",
        headers={"x-api-key": "secret"},
        json={"goal": "   ", "unknown": True},
    )

    assert invalid.status_code == 422
    assert runtime.last_request is None

    invalid_run_id = client.post(
        "/loops/create",
        headers={"x-api-key": "secret"},
        json={"goal": "work", "run_id": "invalid/path"},
    )
    assert invalid_run_id.status_code == 422


def test_queue_create_maps_limits_and_returns_queryable_record() -> None:
    """创建路由应只转换 DTO 并调用队列运行时。"""
    runtime = FakeQueueRuntime()
    client = _client(runtime)
    response = client.post(
        "/loops/create",
        headers={"x-api-key": "secret"},
        json={
            "goal": "  build  ",
            "acceptance_criteria": ["tests pass"],
            "run_id": "run-1",
            "limits": {
                "max_cycles": 3,
                "max_attempts": 7,
                "max_steps_per_plan": 4,
                "timeout_seconds": 30,
            },
            "metadata": {"trace": {"id": "abc"}},
        },
    )

    assert response.status_code == 201
    assert response.json()["run_id"] == "run-1"
    assert response.json()["status"] == "queued"
    assert runtime.last_request is not None
    assert runtime.last_request.goal == "build"
    assert runtime.last_request.limits.max_attempts == 7


def test_queue_list_get_cancel_resume_and_events() -> None:
    """队列运行时应支持全部查询和控制路由。"""
    runtime = FakeQueueRuntime()
    request = LoopRequest("queued work")
    runtime.records["run-1"] = RunRecord("run-1", request)
    runtime.records["run-2"] = RunRecord("run-2", request, status=RunStatus.PAUSED)
    runtime.events["run-2"] = (
        {"id": "event-1", "occurred_at": datetime(2026, 7, 14, tzinfo=timezone.utc)},
    )
    client = _client(runtime)
    headers = {"x-api-key": "secret"}

    listed = client.get("/loops/list?limit=1&offset=1", headers=headers)
    fetched = client.get("/loops/run-2", headers=headers)
    resumed = client.post(
        "/loops/run-2/resume",
        headers=headers,
        json={"mode": "replan"},
    )
    cancelled = client.post("/loops/run-1/cancel", headers=headers)
    events = client.get("/loops/run-2/events/list", headers=headers)

    assert [item["run_id"] for item in listed.json()] == ["run-2"]
    assert fetched.json()["status"] == "paused"
    assert resumed.json()["accepted"] is True
    assert resumed.json()["run"]["status"] == "queued"
    assert cancelled.json() == {"run_id": "run-1", "accepted": True}
    assert events.json()["items"][0]["occurred_at"] == "2026-07-14T00:00:00+00:00"


def test_missing_and_conflicting_runs_have_stable_http_errors() -> None:
    """运行时的缺失与状态冲突异常应映射为 404 和 409。"""
    runtime = FakeQueueRuntime()
    runtime.records["done"] = RunRecord(
        "done",
        LoopRequest("done"),
        status=RunStatus.COMPLETED,
        result=_result("done"),
    )
    client = _client(runtime)
    headers = {"x-api-key": "secret"}

    missing = client.get("/loops/missing", headers=headers)
    conflict = client.post("/loops/done/resume", headers=headers, json={})

    assert missing.status_code == 404
    assert missing.json()["detail"] == "运行不存在"
    assert conflict.status_code == 409
    assert conflict.json()["detail"] == "运行状态与当前操作冲突"


def test_direct_runtime_supports_commands_but_reports_missing_catalog() -> None:
    """直接运行时不应由 API 层伪造运行列表或事件仓储。"""
    client = _client(FakeDirectRuntime())
    headers = {"x-api-key": "secret"}

    created = client.post(
        "/loops/create",
        headers=headers,
        json={"goal": "direct", "run_id": "direct-1"},
    )
    resumed = client.post("/loops/direct-1/resume", headers=headers, json={})
    cancelled = client.post("/loops/direct-1/cancel", headers=headers)

    assert created.status_code == 201
    assert created.json()["status"] == "completed"
    assert resumed.json()["run"]["run_id"] == "direct-1"
    assert cancelled.json()["accepted"] is True
    assert client.get("/loops/list", headers=headers).status_code == 501
    assert client.get("/loops/direct-1", headers=headers).status_code == 501
    assert client.get("/loops/direct-1/events/list", headers=headers).status_code == 501


def test_custom_prefix_is_normalized() -> None:
    """自定义前缀尾部斜杠不应产生双斜杠路由。"""
    client = _client(FakeDirectRuntime(), prefix="/agent-loops/")
    response = client.post(
        "/agent-loops/create",
        headers={"x-api-key": "secret"},
        json={"goal": "custom"},
    )

    assert response.status_code == 201


def test_official_runtimes_match_structural_protocols() -> None:
    """官方异步与队列运行时应能直接传入路由工厂。"""

    class Engine:
        async def run(
            self,
            request: LoopRequest,
            *,
            run_id: str | None = None,
        ) -> LoopResult:
            del request
            return _result(run_id or "engine-run")

        async def resume(
            self,
            run_id: str,
            *,
            mode: ResumeMode = ResumeMode.CONTINUE,
        ) -> LoopResult:
            del mode
            return _result(run_id)

        def cancel(self, run_id: str) -> bool:
            return bool(run_id)

        def create_run_id(self) -> str:
            return "engine-run"

    direct: DirectRuntimeProtocol = AsyncRuntime(Engine())
    queue: QueueRuntimeProtocol = QueueRuntime(
        InMemoryQueueBackend(),
        InMemoryRunRepository(),
    )

    assert isinstance(direct, DirectRuntimeProtocol)
    assert isinstance(queue, QueueRuntimeProtocol)
