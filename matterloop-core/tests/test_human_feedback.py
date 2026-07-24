"""Core 人工反馈闭环与整体目标验收测试。"""

import asyncio
import time

import pytest
from conftest import build_loop
from matterloop_core import (
    AgentLoop,
    ApprovalDecision,
    CompletionAction,
    CompletionDecision,
    HumanAction,
    HumanInteractionKind,
    HumanInteractionNotPendingError,
    HumanInteractionRequest,
    HumanResponse,
    HumanResponseConflictError,
    LoopContext,
    LoopEvent,
    LoopEventType,
    LoopLimits,
    LoopNotResumableError,
    LoopRequest,
    LoopResult,
    LoopStatus,
    Plan,
    PlanStep,
    StopReason,
)


class DeferredApproval:
    """把危险步骤交给公共人工反馈 API 处理。"""

    async def decide(self, step: PlanStep, context: LoopContext) -> ApprovalDecision:
        """始终请求外部人工处理。"""
        del step, context
        return ApprovalDecision.DEFERRED


class CheckpointPropagator:
    """模拟可观测性组件在暂停前写入可恢复关联信息。"""

    def __init__(self) -> None:
        self.prepared_event_types: list[tuple[LoopEventType, ...]] = []

    async def prepare_checkpoint(
        self, context: LoopContext, event_types: tuple[LoopEventType, ...]
    ) -> None:
        self.prepared_event_types.append(event_types)
        if LoopEventType.LOOP_PAUSED in event_types:
            context.propagation_context["traceparent"] = (
                "00-0123456789abcdef0123456789abcdef-0123456789abcdef-01"
            )

    async def publish(self, event: LoopEvent) -> None:
        del event


class ApprovalPlanner:
    """生成一个需要人工审批的固定步骤。"""

    def __init__(self) -> None:
        self.calls = 0
        self.seen_feedback: list[str] = []

    async def plan(self, context: LoopContext) -> Plan:
        """首次生成审批步骤，后续重规划生成安全步骤。"""
        self.calls += 1
        self.seen_feedback.append(context.feedback)
        return Plan(
            (
                PlanStep(
                    f"plan-{self.calls}",
                    requires_approval=self.calls == 1,
                    step_id=f"step-{self.calls}",
                ),
            )
        )


def _pause_for_approval() -> tuple[AgentLoop, ApprovalPlanner, LoopResult, list[LoopEvent]]:
    """组装测试 Loop 并运行到审批暂停点。"""
    loop, store, events = build_loop()
    del store
    observed: list[LoopEvent] = []
    events.subscribe(observed.append)
    planner = ApprovalPlanner()
    loop.planners.register("default", planner, replace=True)
    loop.approval_gate = DeferredApproval()
    paused = asyncio.run(loop.run(LoopRequest("人工审批")))
    return loop, planner, paused, observed


def test_human_approval_resumes_exact_step_without_rechecking_gate() -> None:
    """人工批准后必须复用原计划和步骤游标，且不再次访问审批门。"""
    loop, planner, paused, events = _pause_for_approval()
    assert paused.status is LoopStatus.PAUSED
    assert paused.stop_reason is StopReason.APPROVAL_DEFERRED
    assert paused.pending_interaction is not None
    assert paused.pending_interaction.kind is HumanInteractionKind.APPROVAL

    with pytest.raises(LoopNotResumableError, match="human response"):
        asyncio.run(loop.resume(paused.run_id))

    response = HumanResponse(
        paused.pending_interaction.interaction_id,
        HumanAction.APPROVE,
        idempotency_key="approve-once",
    )
    submitted = asyncio.run(loop.submit_human_response(paused.run_id, response))
    repeated = asyncio.run(loop.submit_human_response(paused.run_id, response))
    resumed = asyncio.run(loop.resume(paused.run_id))

    assert submitted.status is LoopStatus.PAUSED
    assert repeated.revision == submitted.revision
    assert resumed.status is LoopStatus.COMPLETED
    assert resumed.output == "plan-1"
    assert resumed.cycles == 1
    assert planner.calls == 1
    assert len(resumed.human_interactions) == 1
    observed = [event.event_type for event in events]
    assert LoopEventType.HUMAN_INTERACTION_REQUESTED in observed
    assert LoopEventType.HUMAN_RESPONSE_SUBMITTED in observed
    assert LoopEventType.HUMAN_APPROVED in observed
    assert LoopEventType.LOOP_RESUMED in observed


def test_pause_checkpoint_includes_prepared_propagation_context() -> None:
    """Core 必须在保存暂停 checkpoint 前调用可选的关联信息准备器。"""
    loop, store, _ = build_loop()
    propagator = CheckpointPropagator()
    loop.events = propagator
    loop.planners.register("default", ApprovalPlanner(), replace=True)
    loop.approval_gate = DeferredApproval()

    paused = asyncio.run(loop.run(LoopRequest("保存 OTel 父上下文")))

    assert paused.status is LoopStatus.PAUSED
    assert (LoopEventType.LOOP_PAUSED,) in propagator.prepared_event_types
    persisted = store.contexts[paused.run_id]
    assert persisted.propagation_context == {
        "traceparent": "00-0123456789abcdef0123456789abcdef-0123456789abcdef-01"
    }


def test_human_revision_forces_replan_and_preserves_feedback_history() -> None:
    """修改意见应进入规划上下文，并在默认继续模式下强制重新规划。"""
    loop, planner, paused, _ = _pause_for_approval()
    assert paused.pending_interaction is not None
    response = HumanResponse(
        paused.pending_interaction.interaction_id,
        HumanAction.REVISE,
        "改为只读且不需要审批的方案",
        idempotency_key="revise-once",
    )

    asyncio.run(loop.submit_human_response(paused.run_id, response))
    resumed = asyncio.run(loop.resume(paused.run_id))

    assert resumed.status is LoopStatus.COMPLETED
    assert resumed.output == "plan-2"
    assert resumed.cycles == 2
    assert planner.calls == 2
    assert planner.seen_feedback[-1] == response.content
    assert resumed.feedback_history[0].response.content == response.content


def test_rejected_and_conflicting_human_responses_are_structured() -> None:
    """拒绝应进入阻塞状态，同一幂等键不得表达两种意见。"""
    loop, _, paused, _ = _pause_for_approval()
    assert paused.pending_interaction is not None
    rejected = HumanResponse(
        paused.pending_interaction.interaction_id,
        HumanAction.REJECT,
        "风险过高",
        idempotency_key="decision-1",
    )

    result = asyncio.run(loop.submit_human_response(paused.run_id, rejected))

    assert result.status is LoopStatus.BLOCKED
    assert result.stop_reason is StopReason.HUMAN_REJECTED
    with pytest.raises(LoopNotResumableError, match="explicit replan"):
        asyncio.run(loop.resume(paused.run_id))
    with pytest.raises(HumanResponseConflictError):
        asyncio.run(
            loop.submit_human_response(
                paused.run_id,
                HumanResponse(
                    rejected.interaction_id,
                    HumanAction.APPROVE,
                    idempotency_key="decision-1",
                ),
            )
        )
    with pytest.raises(HumanInteractionNotPendingError):
        asyncio.run(
            loop.submit_human_response(
                paused.run_id,
                HumanResponse(
                    rejected.interaction_id,
                    HumanAction.REJECT,
                    idempotency_key="another-key",
                ),
            )
        )


def test_completion_evaluator_can_request_human_without_replaying_step() -> None:
    """整体验收暂停后批准应直接完成，不得重放已经完成的计划步骤。"""
    loop, _, publisher = build_loop()
    events: list[LoopEvent] = []
    publisher.subscribe(events.append)

    class HumanCompletionEvaluator:
        def __init__(self) -> None:
            self.calls = 0

        async def evaluate(self, context: LoopContext) -> CompletionDecision:
            del context
            self.calls += 1
            return CompletionDecision(
                CompletionAction.REQUEST_HUMAN,
                interaction=HumanInteractionRequest(
                    HumanInteractionKind.COMPLETION_REVIEW,
                    "是否接受整体交付？",
                    (HumanAction.APPROVE, HumanAction.REJECT, HumanAction.REVISE),
                ),
            )

    evaluator = HumanCompletionEvaluator()
    loop.completion_evaluator = evaluator
    paused = asyncio.run(loop.run(LoopRequest("整体目标")))
    assert paused.pending_interaction is not None
    assert len(paused.records) == 1

    asyncio.run(
        loop.submit_human_response(
            paused.run_id,
            HumanResponse(paused.pending_interaction.interaction_id, HumanAction.APPROVE),
        )
    )
    completed = asyncio.run(loop.resume(paused.run_id))

    assert completed.status is LoopStatus.COMPLETED
    assert len(completed.records) == 1
    assert evaluator.calls == 1
    observed_types = {event.event_type for event in events}
    assert LoopEventType.COMPLETION_EVALUATION_STARTED in observed_types
    assert LoopEventType.COMPLETION_EVALUATION_COMPLETED in observed_types


def test_completion_evaluator_replans_before_accepting() -> None:
    """整体目标未通过时可开始新 cycle，接受后才进入完成状态。"""
    loop, _, _ = build_loop()

    class ReplanOnceEvaluator:
        def __init__(self) -> None:
            self.calls = 0

        async def evaluate(self, context: LoopContext) -> CompletionDecision:
            del context
            self.calls += 1
            if self.calls == 1:
                return CompletionDecision(CompletionAction.REPLAN, "需要再验证一轮")
            return CompletionDecision(CompletionAction.ACCEPT)

    evaluator = ReplanOnceEvaluator()
    loop.completion_evaluator = evaluator
    result = asyncio.run(loop.run(LoopRequest("整体复核")))

    assert result.status is LoopStatus.COMPLETED
    assert result.cycles == 2
    assert len(result.records) == 2
    assert evaluator.calls == 2


def test_human_wait_does_not_consume_active_timeout() -> None:
    """Loop 暂停期间经过的墙钟时间不得消耗 active timeout。"""
    loop2, _, _ = build_loop()
    planner = ApprovalPlanner()
    loop2.planners.register("default", planner, replace=True)
    loop2.approval_gate = DeferredApproval()
    paused2 = asyncio.run(loop2.run(LoopRequest("短超时", limits=LoopLimits(timeout_seconds=0.03))))
    assert paused2.pending_interaction is not None

    time.sleep(0.05)
    asyncio.run(
        loop2.submit_human_response(
            paused2.run_id,
            HumanResponse(paused2.pending_interaction.interaction_id, HumanAction.APPROVE),
        )
    )
    result = asyncio.run(loop2.resume(paused2.run_id))

    assert result.status is LoopStatus.COMPLETED


def test_event_sequence_is_monotonic_and_checkpointed() -> None:
    """每个审计事件都应携带单调序号并同步写入检查点。"""
    loop, store, events = build_loop()
    observed: list[LoopEvent] = []
    events.subscribe(observed.append)

    result = asyncio.run(loop.run(LoopRequest("事件序号")))

    assert [event.sequence for event in observed] == list(range(1, len(observed) + 1))
    assert result.event_sequence == len(observed)
    assert store.contexts[result.run_id].event_sequence == result.event_sequence
    assert store.contexts[result.run_id].revision == result.revision
