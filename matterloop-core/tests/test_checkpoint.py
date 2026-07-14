"""版本化 Loop 检查点编解码测试。"""

import json
from datetime import datetime, timezone

import pytest
from matterloop_core import (
    ArtifactRef,
    CheckpointSchemaError,
    ExecutionResult,
    HumanAction,
    HumanInteractionKind,
    HumanInteractionRecord,
    HumanInteractionRequest,
    HumanResponse,
    IterationRecord,
    LoopCheckpointCodec,
    LoopContext,
    LoopLimits,
    LoopRequest,
    LoopStatus,
    Plan,
    PlanStep,
    StopReason,
    VerificationResult,
)


def test_checkpoint_round_trip_preserves_resume_state() -> None:
    """JSON 往返后应保留计划位置、预算计数和完整证据。"""
    codec = LoopCheckpointCodec()
    step = PlanStep(
        "构建制品",
        executor="builder",
        acceptance_criteria=("文件存在",),
        requires_approval=True,
        step_id="step-1",
    )
    execution = ExecutionResult(
        "完成",
        artifacts=(ArtifactRef("报告", "artifact://report", "text/markdown", {"tags": ["audit"]}),),
        metadata={"tokens": 12},
    )
    verification = VerificationResult(True, "通过", 98.5, ("测试通过",))
    now = datetime.now(timezone.utc)
    completed_interaction = HumanInteractionRequest(
        HumanInteractionKind.INPUT,
        "需要补充约束",
        (HumanAction.PROVIDE_INPUT,),
        interaction_id="interaction-completed",
        created_at=now,
    )
    pending_interaction = HumanInteractionRequest(
        HumanInteractionKind.APPROVAL,
        "是否发布？",
        (HumanAction.APPROVE, HumanAction.REJECT),
        interaction_id="interaction-pending",
        step_id="step-2",
        created_at=now,
    )
    context = LoopContext(
        request=LoopRequest(
            "完成任务",
            ("可以交付",),
            LoopLimits(3, 7, 4, 60),
            {"trace": {"id": "abc"}},
        ),
        run_id="run-1",
        status=LoopStatus.PAUSED,
        records=[IterationRecord(1, 0, step, execution, verification, 2)],
        feedback="等待确认",
        current_plan=Plan((step, PlanStep("发布", step_id="step-2"))),
        current_step_index=1,
        cycle_count=1,
        total_attempts=2,
        completed_steps=1,
        stop_reason=StopReason.APPROVAL_DEFERRED,
        pending_interaction=pending_interaction,
        human_interactions=[
            HumanInteractionRecord(
                completed_interaction,
                HumanResponse(
                    completed_interaction.interaction_id,
                    HumanAction.PROVIDE_INPUT,
                    "只允许 HTTPS",
                    idempotency_key="response-1",
                    responded_at=now,
                ),
                recorded_at=now,
            )
        ],
        approved_step_ids={"step-1"},
        event_sequence=9,
        revision=8,
        active_elapsed_seconds=1.25,
        started_at=now,
        updated_at=now,
    )

    payload = codec.dumps(context)
    assert json.loads(payload)["schema_version"] == 2

    restored = codec.loads(payload)
    assert restored.run_id == context.run_id
    assert restored.request == context.request
    assert restored.current_plan == context.current_plan
    assert restored.current_step_index == 1
    assert restored.records == context.records
    assert restored.stop_reason is StopReason.APPROVAL_DEFERRED
    assert restored.pending_interaction == pending_interaction
    assert restored.human_interactions == context.human_interactions
    assert restored.approved_step_ids == {"step-1"}
    assert restored.event_sequence == 9
    assert restored.revision == 8
    assert restored.active_elapsed_seconds == 1.25


def test_checkpoint_rejects_unknown_schema_version() -> None:
    """未知版本不得按当前结构猜测解析。"""
    codec = LoopCheckpointCodec()

    with pytest.raises(CheckpointSchemaError, match="unsupported"):
        codec.loads('{"schema_version":1,"context":{}}')


def test_checkpoint_rejects_non_json_metadata() -> None:
    """包含任意 Python 对象的元数据不得进入持久化边界。"""
    codec = LoopCheckpointCodec()
    context = LoopContext(LoopRequest("invalid", metadata={"value": object()}))

    with pytest.raises(CheckpointSchemaError, match="unsupported type"):
        codec.dumps(context)
