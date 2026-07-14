"""同步和异步运行门面的行为测试。"""

from __future__ import annotations

from matterloop_core import (
    HumanAction,
    HumanResponse,
    LoopRequest,
    LoopResult,
    LoopStatus,
    ResumeMode,
)
from matterloop_runtime import AsyncRuntime, LocalRuntime


def _result(run_id: str) -> LoopResult:
    return LoopResult(
        run_id=run_id,
        status=LoopStatus.COMPLETED,
        output="done",
        cycles=1,
        total_attempts=1,
        completed_steps=1,
        records=(),
        stop_reason=None,
    )


class FakeLoopEngine:
    """记录门面委托参数的测试内核。"""

    def __init__(self) -> None:
        self.cancelled: list[str] = []
        self.resume_mode: ResumeMode | None = None
        self.human_responses: list[tuple[str, HumanResponse]] = []

    async def run(self, request: LoopRequest, *, run_id: str | None = None) -> LoopResult:
        del request
        return _result(run_id or "generated")

    async def resume(
        self,
        run_id: str,
        *,
        mode: ResumeMode = ResumeMode.CONTINUE,
    ) -> LoopResult:
        self.resume_mode = mode
        return _result(run_id)

    async def submit_human_response(
        self,
        run_id: str,
        response: HumanResponse,
    ) -> LoopResult:
        self.human_responses.append((run_id, response))
        return _result(run_id)

    def cancel(self, run_id: str) -> bool:
        self.cancelled.append(run_id)
        return True

    def create_run_id(self) -> str:
        return "new-id"


class Resource:
    """记录异步关闭的测试资源。"""

    def __init__(self) -> None:
        self.closed = False

    async def aclose(self) -> None:
        self.closed = True


async def test_async_runtime_delegates_all_operations() -> None:
    engine = FakeLoopEngine()
    runtime = AsyncRuntime(engine)

    assert runtime.create_run_id() == "new-id"
    assert (await runtime.run(LoopRequest("goal"), run_id="run-1")).run_id == "run-1"
    assert (await runtime.resume("run-1", mode=ResumeMode.REPLAN)).run_id == "run-1"
    assert engine.resume_mode is ResumeMode.REPLAN
    response = HumanResponse("interaction", HumanAction.APPROVE)
    assert (await runtime.submit_human_response("run-1", response)).run_id == "run-1"
    assert engine.human_responses == [("run-1", response)]
    assert await runtime.cancel("run-1")
    assert engine.cancelled == ["run-1"]


def test_local_runtime_uses_background_event_loop() -> None:
    engine = FakeLoopEngine()
    resource = Resource()

    with LocalRuntime(AsyncRuntime(engine, resources=[resource])) as runtime:
        assert runtime.create_run_id() == "new-id"
        assert runtime.run(LoopRequest("goal"), run_id="run-2").output == "done"
        assert runtime.resume("run-2").run_id == "run-2"
        response = HumanResponse("interaction", HumanAction.APPROVE)
        assert runtime.submit_human_response("run-2", response).run_id == "run-2"
        assert runtime.cancel("run-2")
    assert resource.closed
