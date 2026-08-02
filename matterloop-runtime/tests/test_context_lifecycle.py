"""Context Lifecycle Engine 的预算、持久化和恢复测试。"""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace

import pytest
from matterloop_core import ExternalStateRef, LoopContext, LoopRequest
from matterloop_models import (
    ContextInputMode,
    MessageRole,
    ModelCapabilities,
    ModelCompactionItem,
    ModelContextScope,
    ModelDescriptor,
    ModelFeature,
    ModelMessage,
    ModelMessageItem,
    ModelRequest,
    ModelResponse,
    ModelToolCallItem,
    ModelToolOutputItem,
    TokenUsage,
    ToolCall,
    ToolOutput,
)
from matterloop_runtime import (
    ContextBudgetExceededError,
    ContextCheckpointEventPublisher,
    ContextEventType,
    ContextLifecycleManager,
    ContextManagedModelClient,
    ContextPolicy,
    ContextSnapshotCodec,
    InMemoryContextBlobStore,
    InMemoryContextStore,
    LocalContextEventPublisher,
    SemanticCompactor,
)


class RecordingModel:
    """记录生命周期包装器实际提交的规范请求。"""

    def __init__(self, responses: list[ModelResponse]) -> None:
        self.responses = responses
        self.requests: list[ModelRequest] = []

    @property
    def descriptor(self) -> ModelDescriptor:
        return ModelDescriptor(
            provider="test",
            model="test-model",
            capabilities=ModelCapabilities(supported=frozenset({ModelFeature.TEXT_GENERATION})),
            context_window_tokens=20_000,
        )

    async def generate(self, request: ModelRequest) -> ModelResponse:
        self.requests.append(request)
        return self.responses.pop(0)


class FixedCounter:
    """按规范输入项数量返回确定值。"""

    async def count(self, request: ModelRequest) -> int:
        return sum(
            10 if isinstance(item, ModelCompactionItem) else 100 for item in request.input_items
        )


class ShrinkingCompactor:
    """把任意历史压缩为一条小摘要消息。"""

    async def compact(self, items, *, scope, target_tokens):
        del scope, target_tokens
        from matterloop_models import ModelCompactionItem

        return (
            ModelCompactionItem(
                payload='{"facts":[]}',
                provider="summary",
                model="small",
                native=False,
                metadata={"source_item_ids": tuple(item.item_id for item in items)},
            ),
        )


class NativeRecordingModel(RecordingModel):
    """模拟返回不透明下一窗口的原生压缩模型。"""

    @property
    def descriptor(self) -> ModelDescriptor:
        return ModelDescriptor(
            provider="native-provider",
            model="native-model",
            capabilities=ModelCapabilities(
                supported=frozenset(
                    {
                        ModelFeature.TEXT_GENERATION,
                        ModelFeature.NATIVE_COMPACTION,
                    }
                )
            ),
            context_window_tokens=400,
        )

    async def compact_input(self, request: ModelRequest):
        del request
        return (
            ModelCompactionItem(
                payload='{"encrypted_content":"opaque","type":"compaction"}',
                provider="native-provider",
                model="native-model",
                native=True,
            ),
        )


def test_semantic_compactor_uses_trusted_source_ids() -> None:
    async def scenario() -> None:
        source = ModelMessageItem(
            MessageRole.USER,
            "historical context",
            item_id="source-item",
        )
        model = RecordingModel(
            [
                ModelResponse(
                    output_text=json.dumps(
                        {
                            "objective": "完成任务",
                            "constraints": [],
                            "completed": [],
                            "decisions": [],
                            "failures": [],
                            "current_state": "执行中",
                            "pending": [],
                            "artifacts": [],
                            "facts": [],
                            "source_item_ids": ["model-generated-wrong-id"],
                        }
                    )
                )
            ]
        )
        compactor = SemanticCompactor(
            model,
            provider="test",
            model_name="test-model",
        )

        compacted = await compactor.compact(
            (source,),
            scope=ModelContextScope("run-source-ids", "worker"),
            target_tokens=100,
        )

        summary = json.loads(compacted[0].payload)
        assert summary["source_item_ids"] == ["source-item"]
        assert compacted[0].metadata["source_item_ids"] == ("source-item",)
        schema = model.requests[0].response_schema
        assert schema is not None
        assert "source_item_ids" not in schema["properties"]

    asyncio.run(scenario())


def test_managed_client_persists_canonical_tool_conversation() -> None:
    async def scenario() -> None:
        model = RecordingModel(
            [
                ModelResponse(
                    tool_calls=(ToolCall("call-1", "lookup", {"query": "x"}),),
                    usage=TokenUsage(input_tokens=10),
                ),
                ModelResponse(
                    output_text="done",
                    usage=TokenUsage(input_tokens=12, total_tokens=12),
                ),
            ]
        )
        store = InMemoryContextStore()
        manager = ContextLifecycleManager(
            ContextPolicy(max_context_tokens=20_000, reserved_output_tokens=10),
            store,
            InMemoryContextBlobStore(),
        )
        client = ContextManagedModelClient(model, manager)
        scope = ModelContextScope("run-1", "worker", task_id="step-1")

        first = await client.generate(
            ModelRequest(
                messages=(
                    ModelMessage(MessageRole.DEVELOPER, "execute"),
                    ModelMessage(MessageRole.USER, '{"goal":"find","history":["old"]}'),
                ),
                context_scope=scope,
            )
        )
        assert first.output_items

        second = await client.generate(
            ModelRequest(
                tool_outputs=(ToolOutput("call-1", "value"),),
                context_scope=scope,
                context_mode=ContextInputMode.APPEND,
            )
        )

        assert second.output_text == "done"
        assert all(request.input_items for request in model.requests)
        assert not any(request.continuation for request in model.requests)
        snapshot = await store.load(scope.key)
        assert snapshot is not None
        assert snapshot.revision == 4
        assert any(isinstance(item, ModelToolCallItem) for item in snapshot.input_items)
        assert any(isinstance(item, ModelToolOutputItem) for item in snapshot.input_items)
        assert snapshot.metadata["last_model_usage"] == {
            "input_tokens": 12,
            "output_tokens": 0,
            "total_tokens": 12,
            "cache_hit_tokens": 0,
            "cache_miss_tokens": 0,
            "reasoning_tokens": 0,
        }

    asyncio.run(scenario())


def test_large_tool_result_is_externalized_before_model_call() -> None:
    async def scenario() -> None:
        model = RecordingModel([ModelResponse(output_text="ok")])
        store = InMemoryContextStore()
        blobs = InMemoryContextBlobStore()
        events = LocalContextEventPublisher()
        client = ContextManagedModelClient(
            model,
            ContextLifecycleManager(
                ContextPolicy(
                    max_context_tokens=20_000,
                    tool_result_inline_tokens=10,
                    reserved_output_tokens=10,
                ),
                store,
                blobs,
                events=events,
            ),
        )
        scope = ModelContextScope("run-2", "worker", task_id="step-1")
        seed_model = RecordingModel([ModelResponse(tool_calls=(ToolCall("call-1", "lookup"),))])
        seed_client = ContextManagedModelClient(
            seed_model,
            ContextLifecycleManager(
                ContextPolicy(max_context_tokens=20_000, reserved_output_tokens=10),
                store,
                blobs,
            ),
        )
        await seed_client.generate(
            ModelRequest(
                messages=(ModelMessage(MessageRole.USER, "goal"),),
                context_scope=scope,
            )
        )
        output = "large-result-" * 500
        await client.generate(
            ModelRequest(
                tool_outputs=(ToolOutput("call-1", output),),
                context_scope=scope,
                context_mode=ContextInputMode.APPEND,
            )
        )

        sent = next(
            item for item in model.requests[0].input_items if isinstance(item, ModelToolOutputItem)
        )
        assert sent.artifact_uri is not None
        assert output not in sent.output
        assert await blobs.get(sent.artifact_uri) == output.encode()
        assert any(
            event.event_type is ContextEventType.TOOL_RESULT_EXTERNALIZED for event in events.events
        )

    asyncio.run(scenario())


def test_hard_threshold_blocks_when_only_pinned_items_remain() -> None:
    async def scenario() -> None:
        model = RecordingModel([ModelResponse(output_text="unused")])
        client = ContextManagedModelClient(
            model,
            ContextLifecycleManager(
                ContextPolicy(
                    max_context_tokens=100,
                    reserved_output_tokens=50,
                    recent_turns=1,
                ),
                InMemoryContextStore(),
                InMemoryContextBlobStore(),
                token_counter=FixedCounter(),
            ),
        )
        with pytest.raises(ContextBudgetExceededError):
            await client.generate(
                ModelRequest(
                    messages=(
                        ModelMessage(MessageRole.SYSTEM, "system"),
                        ModelMessage(MessageRole.USER, "goal"),
                    ),
                    context_scope=ModelContextScope("run-3", "planner"),
                )
            )
        assert model.requests == []

    asyncio.run(scenario())


def test_semantic_compaction_reduces_historical_payload() -> None:
    async def scenario() -> None:
        model = RecordingModel([ModelResponse(output_text="ok")])
        store = InMemoryContextStore()
        client = ContextManagedModelClient(
            model,
            ContextLifecycleManager(
                ContextPolicy(
                    max_context_tokens=400,
                    reserved_output_tokens=10,
                    recent_turns=1,
                ),
                store,
                InMemoryContextBlobStore(),
                semantic_compactor=ShrinkingCompactor(),
                token_counter=FixedCounter(),
            ),
        )
        await client.generate(
            ModelRequest(
                messages=(
                    ModelMessage(MessageRole.SYSTEM, "system"),
                    ModelMessage(
                        MessageRole.USER,
                        '{"goal":"g","acceptance_criteria":[],"history":["a","b"]}',
                    ),
                ),
                context_scope=ModelContextScope("run-4", "planner"),
            )
        )
        snapshot = await store.load("default:run-4:planner:-")
        assert snapshot is not None
        assert snapshot.compaction_count == 1
        assert snapshot.archive_uris

    asyncio.run(scenario())


def test_model_switch_rebuilds_native_state_from_archived_history() -> None:
    async def scenario() -> None:
        store = InMemoryContextStore()
        manager = ContextLifecycleManager(
            ContextPolicy(
                max_context_tokens=400,
                reserved_output_tokens=10,
                recent_turns=1,
            ),
            store,
            InMemoryContextBlobStore(),
            semantic_compactor=ShrinkingCompactor(),
            token_counter=FixedCounter(),
        )
        scope = ModelContextScope("run-switch", "worker")
        first = await manager.prepare(
            ModelRequest(
                messages=(
                    ModelMessage(MessageRole.SYSTEM, "system"),
                    ModelMessage(
                        MessageRole.USER,
                        '{"goal":"g","history":["old"]}',
                    ),
                ),
                context_scope=scope,
            ),
            NativeRecordingModel([]),
        )
        assert any(
            isinstance(item, ModelCompactionItem) and item.native
            for item in first.snapshot.input_items
        )

        switched = await manager.prepare(
            ModelRequest(
                input_items=(ModelMessageItem(MessageRole.USER, "continue"),),
                context_scope=scope,
                context_mode=ContextInputMode.APPEND,
            ),
            RecordingModel([]),
        )

        assert any(
            isinstance(item, ModelCompactionItem) and not item.native
            for item in switched.snapshot.input_items
        )
        assert not any(
            isinstance(item, ModelCompactionItem) and item.native
            for item in switched.snapshot.input_items
        )

    asyncio.run(scenario())


def test_snapshot_codec_and_exact_restore_detect_tampering() -> None:
    async def scenario() -> None:
        model = RecordingModel([ModelResponse(output_text="ok")])
        store = InMemoryContextStore()
        manager = ContextLifecycleManager(
            ContextPolicy(max_context_tokens=20_000, reserved_output_tokens=10),
            store,
            InMemoryContextBlobStore(),
        )
        prepared = await manager.prepare(
            ModelRequest(
                messages=(ModelMessage(MessageRole.USER, "goal"),),
                context_scope=ModelContextScope("run-5", "planner"),
            ),
            model,
        )
        restored = await manager.restore(prepared.snapshot_ref)
        assert ContextSnapshotCodec().dumps(restored)
        bad = replace(prepared.snapshot_ref, checksum="0" * 64)
        with pytest.raises(Exception, match="checksum"):
            await manager.restore(bad)

    asyncio.run(scenario())


def test_recovery_uses_checkpoint_revision_instead_of_newer_latest() -> None:
    async def scenario() -> None:
        store = InMemoryContextStore()
        manager = ContextLifecycleManager(
            ContextPolicy(max_context_tokens=20_000, reserved_output_tokens=10),
            store,
            InMemoryContextBlobStore(),
        )
        model = RecordingModel([])
        scope = ModelContextScope("run-recovery", "planner")
        checkpoint = await manager.prepare(
            ModelRequest(
                messages=(ModelMessage(MessageRole.USER, "checkpoint-state"),),
                context_scope=scope,
            ),
            model,
        )
        orphan = await manager.prepare(
            ModelRequest(
                messages=(ModelMessage(MessageRole.USER, "orphan-state"),),
                context_scope=scope,
            ),
            model,
        )
        assert orphan.snapshot.revision == 2

        manager.register_recovery_references((checkpoint.snapshot_ref,))
        recovered = await manager.prepare(
            ModelRequest(
                input_items=(ModelMessageItem(MessageRole.USER, "continue"),),
                context_scope=scope,
                context_mode=ContextInputMode.APPEND,
            ),
            model,
        )

        contents = [
            item.content
            for item in recovered.snapshot.input_items
            if isinstance(item, ModelMessageItem)
        ]
        assert recovered.snapshot.revision == 3
        assert "checkpoint-state" in contents
        assert "orphan-state" not in contents

    asyncio.run(scenario())


def test_checkpoint_publisher_records_latest_context_reference() -> None:
    class Delegate:
        async def publish(self, event) -> None:
            del event

    async def scenario() -> None:
        model = RecordingModel([ModelResponse(output_text="ok")])
        manager = ContextLifecycleManager(
            ContextPolicy(max_context_tokens=20_000, reserved_output_tokens=10),
            InMemoryContextStore(),
            InMemoryContextBlobStore(),
        )
        prepared = await manager.prepare(
            ModelRequest(
                messages=(ModelMessage(MessageRole.USER, "goal"),),
                context_scope=ModelContextScope("run-6", "planner"),
            ),
            model,
        )
        assert prepared.snapshot_ref.revision == 1
        publisher = ContextCheckpointEventPublisher(Delegate(), manager)
        context = LoopContext(LoopRequest("goal"), run_id="run-6")
        await publisher.prepare_checkpoint(context, ())
        assert context.external_state_refs == [
            ExternalStateRef(
                kind="model_context",
                key=prepared.snapshot_ref.key,
                revision=prepared.snapshot_ref.revision,
                checksum=prepared.snapshot_ref.checksum,
            )
        ]

    asyncio.run(scenario())
