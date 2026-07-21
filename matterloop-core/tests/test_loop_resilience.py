"""Loop 心跳、取消、恢复和提交幂等性测试。"""

import asyncio
from datetime import datetime, timezone

import pytest
from conftest import build_loop
from matterloop_core import (
    ExecutionResult,
    LoopContext,
    LoopEvent,
    LoopEventType,
    LoopLimits,
    LoopRequest,
    LoopRequestConflictError,
    LoopStatus,
    Plan,
    PlanStep,
    RetryAction,
    RetryDecision,
    StopReason,
)


@pytest.mark.parametrize("timeout", [float("nan"), float("inf"), float("-inf")])
def test_loop_timeout_requires_finite_value(timeout: float) -> None:
    """非有限超时不得进入调度器形成永久等待。"""
    with pytest.raises(ValueError, match="finite"):
        LoopLimits(timeout_seconds=timeout)


def test_long_component_emits_heartbeat_and_can_be_cancelled() -> None:
    """长调用必须持续心跳，取消后还要回收组件任务并保存终态。"""

    async def scenario() -> None:
        loop, store, events = build_loop()
        loop.heartbeat_interval_seconds = 0.01
        loop.cancellation_poll_interval_seconds = 0.005
        started = asyncio.Event()
        cancelled = asyncio.Event()
        observed: list[LoopEventType] = []

        class BlockingExecutor:
            async def execute(
                self,
                step: PlanStep,
                context: LoopContext,
            ) -> ExecutionResult:
                del step, context
                started.set()
                try:
                    await asyncio.Event().wait()
                finally:
                    cancelled.set()
                return ExecutionResult("unreachable")

        def collect(event: LoopEvent) -> None:
            observed.append(event.event_type)

        events.subscribe(collect)
        loop.executors.register("default", BlockingExecutor(), replace=True)
        run_id = loop.create_run_id()
        running = asyncio.create_task(loop.run(LoopRequest("long-running"), run_id=run_id))
        await started.wait()
        while LoopEventType.LOOP_HEARTBEAT not in observed:
            await asyncio.sleep(0.005)

        assert loop.cancel(run_id)
        result = await asyncio.wait_for(running, timeout=0.5)

        assert result.status is LoopStatus.CANCELLED
        assert result.stop_reason is StopReason.CANCELLED
        assert cancelled.is_set()
        assert store.contexts[run_id].status is LoopStatus.CANCELLED
        assert store.contexts[run_id].last_heartbeat_at is not None

    asyncio.run(scenario())


def test_component_timeout_error_is_not_misclassified_as_loop_timeout() -> None:
    """组件自身的 TimeoutError 应按组件失败处理，而不是伪装成总超时。"""
    loop, store, _ = build_loop()

    class TimeoutExecutor:
        async def execute(
            self,
            step: PlanStep,
            context: LoopContext,
        ) -> ExecutionResult:
            del step, context
            raise TimeoutError("provider timed out")

    loop.executors.register("default", TimeoutExecutor(), replace=True)
    with pytest.raises(TimeoutError, match="provider timed out"):
        asyncio.run(loop.run(LoopRequest("component-timeout"), run_id="component-timeout"))

    checkpoint = store.contexts["component-timeout"]
    assert checkpoint.status is LoopStatus.FAILED
    assert checkpoint.stop_reason is StopReason.COMPONENT_ERROR


def test_external_task_cancellation_persists_cancelled_terminal_state() -> None:
    """宿主取消 asyncio Task 时不得留下 EXECUTING 临时状态。"""

    async def scenario() -> None:
        loop, store, _ = build_loop()
        loop.cancellation_poll_interval_seconds = 0.005
        started = asyncio.Event()

        class BlockingExecutor:
            async def execute(
                self,
                step: PlanStep,
                context: LoopContext,
            ) -> ExecutionResult:
                del step, context
                started.set()
                await asyncio.Event().wait()
                return ExecutionResult("unreachable")

        loop.executors.register("default", BlockingExecutor(), replace=True)
        running = asyncio.create_task(loop.run(LoopRequest("host-cancel"), run_id="host-cancel"))
        await started.wait()
        running.cancel()
        with pytest.raises(asyncio.CancelledError):
            await running

        checkpoint = store.contexts["host-cancel"]
        assert checkpoint.status is LoopStatus.CANCELLED
        assert checkpoint.stop_reason is StopReason.CANCELLED

    asyncio.run(scenario())


def test_run_id_makes_repeated_submission_idempotent() -> None:
    """相同 run_id 与请求只执行一次，不同请求则明确冲突。"""
    loop, _, _ = build_loop()
    calls = 0

    class CountingExecutor:
        async def execute(
            self,
            step: PlanStep,
            context: LoopContext,
        ) -> ExecutionResult:
            nonlocal calls
            del step, context
            calls += 1
            return ExecutionResult("once")

    loop.executors.register("default", CountingExecutor(), replace=True)
    request = LoopRequest("idempotent")
    first = asyncio.run(loop.run(request, run_id="stable-run"))
    repeated = asyncio.run(loop.run(request, run_id="stable-run"))

    assert first == repeated
    assert calls == 1
    with pytest.raises(LoopRequestConflictError):
        asyncio.run(loop.run(LoopRequest("different"), run_id="stable-run"))


def test_executor_retry_reuses_stable_operation_id() -> None:
    """执行器重试必须获得同一操作标识，供下游按键去重。"""
    loop, _, _ = build_loop()
    operation_ids: list[str | None] = []

    class RetryOnce:
        def decide(
            self,
            error: Exception,
            attempt: int,
            context: LoopContext,
        ) -> RetryDecision:
            del error, context
            return RetryDecision(RetryAction.RETRY if attempt == 1 else RetryAction.FAIL)

    class FlakyExecutor:
        async def execute(
            self,
            step: PlanStep,
            context: LoopContext,
        ) -> ExecutionResult:
            del step
            operation_ids.append(context.active_operation_id)
            if len(operation_ids) == 1:
                raise RuntimeError("transient")
            return ExecutionResult("done")

    loop.retry_policy = RetryOnce()
    loop.executors.register("default", FlakyExecutor(), replace=True)
    result = asyncio.run(loop.run(LoopRequest("retry"), run_id="retry-run"))

    assert result.status is LoopStatus.COMPLETED
    assert operation_ids == ["retry-run:1:" + result.records[0].step.step_id] * 2


def test_recover_verifies_persisted_execution_without_resubmitting() -> None:
    """执行结果已落盘时，重启恢复只验证结果，不再次调用执行器。"""
    loop, store, _ = build_loop()
    step = PlanStep("persisted", step_id="step-persisted")
    context = LoopContext(
        request=LoopRequest("recover"),
        run_id="recover-verifying",
        status=LoopStatus.VERIFYING,
        current_plan=Plan((step,)),
        cycle_count=1,
        total_attempts=1,
        active_operation_id="recover-verifying:1:step-persisted",
        pending_execution=ExecutionResult("already-computed"),
        pending_attempt=1,
        active_started_at=datetime.now(timezone.utc),
    )
    asyncio.run(store.save(context))

    class ForbiddenExecutor:
        async def execute(
            self,
            step: PlanStep,
            context: LoopContext,
        ) -> ExecutionResult:
            del step, context
            raise AssertionError("executor must not be called during recovery")

    loop.executors.register("default", ForbiddenExecutor(), replace=True)
    result = asyncio.run(loop.recover("recover-verifying"))

    assert result.status is LoopStatus.COMPLETED
    assert result.output == "already-computed"
    assert result.total_attempts == 1


def test_recover_ambiguous_execution_blocks_without_resubmitting() -> None:
    """无法确认结果的执行中检查点必须阻塞，而不是冒险重复提交。"""
    loop, store, _ = build_loop()
    step = PlanStep("ambiguous", step_id="step-ambiguous")
    context = LoopContext(
        request=LoopRequest("recover"),
        run_id="recover-executing",
        status=LoopStatus.EXECUTING,
        current_plan=Plan((step,)),
        cycle_count=1,
        total_attempts=1,
        active_operation_id="recover-executing:1:step-ambiguous",
        pending_attempt=1,
        active_started_at=datetime.now(timezone.utc),
    )
    asyncio.run(store.save(context))

    result = asyncio.run(loop.recover("recover-executing"))

    assert result.status is LoopStatus.BLOCKED
    assert result.stop_reason is StopReason.RECOVERY_REQUIRED
    assert result.active_operation_id == "recover-executing:1:step-ambiguous"
