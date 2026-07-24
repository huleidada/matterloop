"""四类预设的真实组件装配与 fake 模型端到端测试。"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping, Sequence
from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest
from matterloop_core import (
    ApprovalDecision,
    ArtifactRef,
    ExecutionResult,
    LoopContext,
    LoopEvent,
    LoopLimits,
    LoopRequest,
    LoopStatus,
    PlanStep,
    StopReason,
)
from matterloop_memory import InMemoryCheckpointStore
from matterloop_models import (
    FakeModelClient,
    ModelResponse,
    ToolCall,
)
from matterloop_models.providers import OpenAIModelClient, OpenAIModelConfig
from matterloop_observability import Score, SpanRecord
from matterloop_observability.pipeline import ExportItem
from matterloop_presets import (
    CodingPresetConfig,
    MinimalPresetConfig,
    PresetConfigurationError,
    ProductionLocalRuntime,
    ResearchPresetConfig,
    build_coding_local_runtime,
    build_coding_runtime,
    build_minimal_local_runtime,
    build_minimal_runtime,
    build_production_local_runtime,
    build_production_runtime,
    build_research_local_runtime,
    build_research_runtime,
)
from matterloop_runtime import InMemoryQueueBackend, InMemoryRunRepository, LocalRuntime, RunStatus
from matterloop_tools import ToolContext, ToolPermissionDeniedError


def _plan_response(
    *,
    executor: str = "default",
    requires_approval: bool = False,
    description: str = "完成任务",
) -> ModelResponse:
    """创建符合标准 Planner Schema 的模型响应。"""
    return ModelResponse(
        output_text=json.dumps(
            {
                "steps": [
                    {
                        "description": description,
                        "executor": executor,
                        "acceptance_criteria": ["结果可验证"],
                        "requires_approval": requires_approval,
                    }
                ]
            },
            ensure_ascii=False,
        )
    )


def _verification_response(*, evidence: tuple[str, ...] = ("artifact://result",)) -> ModelResponse:
    """创建符合标准 Verifier Schema 的通过响应。"""
    return ModelResponse(
        output_text=json.dumps(
            {
                "passed": True,
                "score": 95,
                "feedback": "验证通过",
                "evidence": list(evidence),
                "failed_criteria": [],
            },
            ensure_ascii=False,
        )
    )


def _simple_success_model(*, evidence: tuple[str, ...] = ("artifact://result",)) -> FakeModelClient:
    """创建完成规划、执行、验证三次调用的假模型。"""
    return FakeModelClient(
        (
            _plan_response(),
            ModelResponse(output_text="执行完成"),
            _verification_response(evidence=evidence),
        )
    )


def test_minimal_runtime_executes_full_loop_without_tools() -> None:
    """最小预设应使用内存检查点完成完整三 Agent 闭环。"""

    async def scenario() -> None:
        runtime = build_minimal_runtime(_simple_success_model())
        try:
            result = await runtime.run(LoopRequest("最小任务"))
            checkpoint = await runtime.checkpoint_store.load(result.run_id)

            assert result.status is LoopStatus.COMPLETED
            assert runtime.tool_registries["default"].names() == ()
            assert checkpoint is not None
            assert checkpoint.status is LoopStatus.COMPLETED
        finally:
            await runtime.aclose()

    asyncio.run(scenario())


def test_preset_close_does_not_close_shared_injected_model_client() -> None:
    """预设关闭模型适配器时不得误关调用方共享的 SDK 客户端。"""

    class SharedSDKClient:
        def __init__(self) -> None:
            self.closed = False

        async def close(self) -> None:
            self.closed = True

    async def scenario() -> None:
        sdk_client = SharedSDKClient()
        model = OpenAIModelClient(
            OpenAIModelConfig(model="configured-model"),
            client=sdk_client,
        )
        runtime = build_minimal_runtime(model)

        await runtime.aclose()

        assert not sdk_client.closed

    asyncio.run(scenario())


def test_coding_runtime_forces_approval_and_executes_real_write(tmp_path: Path) -> None:
    """高权限编码步骤即使模型漏标审批，也必须审批后才能写入工作区。"""

    class RecordingApproval:
        def __init__(self) -> None:
            self.steps: list[PlanStep] = []

        async def decide(self, step: PlanStep, context: LoopContext) -> ApprovalDecision:
            del context
            self.steps.append(step)
            return ApprovalDecision.APPROVED

    async def scenario() -> None:
        model = FakeModelClient(
            (
                _plan_response(executor="coding", requires_approval=False),
                ModelResponse(
                    tool_calls=(
                        ToolCall(
                            "write-1",
                            "filesystem",
                            {
                                "operation": "write",
                                "path": "result.txt",
                                "content": "written by preset",
                            },
                        ),
                    ),
                    response_id="worker-1",
                ),
                ModelResponse(output_text="文件已写入"),
                _verification_response(),
            )
        )
        approval = RecordingApproval()
        runtime = build_coding_runtime(model, tmp_path, approval_gate=approval)
        try:
            result = await runtime.run(LoopRequest("写入结果"))

            assert result.status is LoopStatus.COMPLETED
            assert (tmp_path / "result.txt").read_text() == "written by preset"
            assert approval.steps and approval.steps[0].requires_approval
            assert runtime.tool_registries["default"].names() == ("filesystem",)
            assert runtime.tool_registries["coding"].names() == ("filesystem", "shell")
        finally:
            await runtime.aclose()

    asyncio.run(scenario())


def test_coding_default_gate_pauses_privileged_step(tmp_path: Path) -> None:
    """未接入人工审批实现时，高权限步骤应安全暂停。"""

    async def scenario() -> None:
        runtime = build_coding_runtime(
            FakeModelClient((_plan_response(executor="coding"),)),
            tmp_path,
        )
        try:
            result = await runtime.run(LoopRequest("危险任务"))
            assert result.status is LoopStatus.PAUSED
            assert result.stop_reason is StopReason.APPROVAL_DEFERRED
        finally:
            await runtime.aclose()

    asyncio.run(scenario())


def test_research_runtime_is_read_only_and_requires_citation(tmp_path: Path) -> None:
    """研究预设应允许读取资料、拒绝写入并接受带引用的结果。"""
    (tmp_path / "source.txt").write_text("source material")

    async def scenario() -> None:
        model = FakeModelClient(
            (
                _plan_response(description="读取资料并总结"),
                ModelResponse(
                    tool_calls=(
                        ToolCall(
                            "read-1",
                            "filesystem",
                            {"operation": "read", "path": "source.txt"},
                        ),
                    ),
                    response_id="research-1",
                ),
                ModelResponse(output_text="研究结论及来源"),
                _verification_response(evidence=("https://example.com/source",)),
            )
        )
        config = ResearchPresetConfig(allowed_hosts=frozenset({"example.com"}))
        runtime = build_research_runtime(model, tmp_path, config)
        try:
            result = await runtime.run(LoopRequest("研究问题"))
            assert result.status is LoopStatus.COMPLETED
            assert runtime.tool_registries["default"].names() == ("filesystem", "http")
            with pytest.raises(ToolPermissionDeniedError):
                await runtime.tool_registries["default"].invoke(
                    "filesystem",
                    {"operation": "write", "path": "blocked.txt", "content": "no"},
                    context=ToolContext("run-1"),
                )
        finally:
            await runtime.aclose()

    asyncio.run(scenario())


def test_research_verifier_downgrades_result_without_citation(tmp_path: Path) -> None:
    """模型即使声称通过，没有引用证据时仍应验证失败。"""

    async def scenario() -> None:
        runtime = build_research_runtime(
            _simple_success_model(evidence=()),
            tmp_path,
            ResearchPresetConfig(allowed_hosts=frozenset({"example.com"})),
        )
        try:
            result = await runtime.run(
                LoopRequest(
                    "无引用研究",
                    limits=LoopLimits(max_cycles=1, max_attempts=3, max_steps_per_plan=2),
                )
            )
            assert result.status is LoopStatus.BLOCKED
            assert result.stop_reason is StopReason.CYCLE_LIMIT
            assert result.records[0].verification.failed_criteria == ("引用证据",)
        finally:
            await runtime.aclose()

    asyncio.run(scenario())


def test_research_verifier_accepts_execution_artifact_uri(tmp_path: Path) -> None:
    """验证证据为空时，执行结果中的可追溯制品 URI 也应满足引用要求。"""

    class ArtifactExecutor:
        async def execute(self, step: PlanStep, context: LoopContext) -> ExecutionResult:
            del step, context
            return ExecutionResult(
                "研究结果",
                artifacts=(
                    ArtifactRef(
                        name="来源",
                        uri="https://example.com/source",
                        media_type="text/html",
                    ),
                ),
            )

    async def scenario() -> None:
        model = FakeModelClient(
            (
                _plan_response(description="读取资料并生成制品"),
                _verification_response(evidence=()),
            )
        )
        runtime = build_research_runtime(
            model,
            tmp_path,
            ResearchPresetConfig(allowed_hosts=frozenset({"example.com"})),
        )
        runtime.loop.executors.register("default", ArtifactExecutor(), replace=True)
        try:
            result = await runtime.run(LoopRequest("制品引用研究"))

            assert result.status is LoopStatus.COMPLETED
            assert result.records[0].execution.artifacts[0].uri.startswith("https://")
            assert result.records[0].verification.evidence == ()
        finally:
            await runtime.aclose()

    asyncio.run(scenario())


def test_production_runtime_requires_and_uses_explicit_infrastructure() -> None:
    """生产预设不得回退内存依赖，显式依赖应进入队列和 worker。"""

    class AuditPublisher:
        def __init__(self) -> None:
            self.events: list[LoopEvent] = []

        async def publish(self, event: LoopEvent) -> None:
            self.events.append(event)

        async def list_events(
            self,
            run_id: str,
            *,
            after: str | None = None,
            limit: int = 100,
        ) -> tuple[Mapping[str, object], ...]:
            del after
            return tuple(
                {"event_type": event.event_type.value, "run_id": event.context.run_id}
                for event in self.events
                if event.context.run_id == run_id
            )[:limit]

    with pytest.raises(PresetConfigurationError, match="queue_backend"):
        build_production_runtime(FakeModelClient())

    async def scenario() -> None:
        queue = InMemoryQueueBackend()
        repository = InMemoryRunRepository()
        checkpoints = InMemoryCheckpointStore()
        audit = AuditPublisher()
        runtime = build_production_runtime(
            _simple_success_model(),
            queue_backend=queue,
            run_repository=repository,
            checkpoint_store=checkpoints,
            audit_publisher=audit,
        )
        try:
            queued_id = await runtime.submit(LoopRequest("queued"), run_id="queued-1")
            queued = await runtime.get(queued_id)
            worker_result = await runtime.worker_runtime.run(
                LoopRequest("worker"),
                run_id="worker-1",
            )
            saved = await checkpoints.load("worker-1")
            events = await runtime.list_events("worker-1")

            assert queued is not None and queued.status is RunStatus.QUEUED
            assert worker_result.status is LoopStatus.COMPLETED
            assert saved is not None and saved.status is LoopStatus.COMPLETED
            assert audit.events
            assert events
        finally:
            await runtime.aclose()

    asyncio.run(scenario())


class _CollectingSpanExporter:
    """收集生产预设 tracing 装配导出批次的导出器。"""

    def __init__(self) -> None:
        self.items: list[ExportItem] = []

    def export(self, batch: Sequence[ExportItem]) -> None:
        """记录一批跨度与评分。"""
        self.items.extend(batch)


def test_production_runtime_traces_spans_when_exporter_provided() -> None:
    """提供 trace_exporter 时事件流应产生跨度树，并在关闭运行时时排空。"""

    async def scenario() -> None:
        exporter = _CollectingSpanExporter()
        runtime = build_production_runtime(
            _simple_success_model(),
            queue_backend=InMemoryQueueBackend(),
            run_repository=InMemoryRunRepository(),
            checkpoint_store=InMemoryCheckpointStore(),
            audit_publisher=_NoopPublisher(),
            trace_exporter=exporter,
        )
        result = await runtime.worker_runtime.run(LoopRequest("worker"), run_id="worker-trace-1")
        # 默认批量阈值不会在一次运行内触发导出，关闭运行时必须负责排空流水线。
        await runtime.aclose()

        assert result.status is LoopStatus.COMPLETED
        spans = [item for item in exporter.items if isinstance(item, SpanRecord)]
        assert spans
        assert {span.trace_id for span in spans} == {"worker-trace-1"}
        assert any(span.parent_span_id is None for span in spans)
        assert any(span.observation_type == "generation" for span in spans)
        scores = [item for item in exporter.items if isinstance(item, Score)]
        assert scores
        assert all(score.name == "verification" for score in scores)

    asyncio.run(scenario())


def test_production_runtime_without_exporter_keeps_event_pipeline_unchanged() -> None:
    """默认不装 tracing 时生产预设不应产生跨度或引入额外线程资源。"""

    async def scenario() -> None:
        audit = _NoopPublisher()
        runtime = build_production_runtime(
            _simple_success_model(),
            queue_backend=InMemoryQueueBackend(),
            run_repository=InMemoryRunRepository(),
            checkpoint_store=InMemoryCheckpointStore(),
            audit_publisher=audit,
        )
        result = await runtime.worker_runtime.run(LoopRequest("worker"), run_id="worker-plain-1")
        await runtime.aclose()

        assert result.status is LoopStatus.COMPLETED

    asyncio.run(scenario())


def test_all_local_builders_return_closable_sync_facades(tmp_path: Path) -> None:
    """每个异步预设都应提供对应的同步本地构建函数。"""
    minimal = build_minimal_local_runtime(FakeModelClient())
    coding = build_coding_local_runtime(FakeModelClient(), tmp_path)
    research = build_research_local_runtime(
        FakeModelClient(),
        tmp_path,
        ResearchPresetConfig(allowed_hosts=frozenset({"example.com"})),
    )
    production = build_production_local_runtime(
        FakeModelClient(),
        queue_backend=InMemoryQueueBackend(),
        run_repository=InMemoryRunRepository(),
        checkpoint_store=InMemoryCheckpointStore(),
        audit_publisher=_NoopPublisher(),
    )
    try:
        assert isinstance(minimal, LocalRuntime)
        assert isinstance(coding, LocalRuntime)
        assert isinstance(research, LocalRuntime)
        assert isinstance(production, ProductionLocalRuntime)
    finally:
        minimal.close()
        coding.close()
        research.close()
        production.close()


def test_configs_are_frozen_and_validate_security_boundaries() -> None:
    """调用方不得在运行中修改配置，研究主机白名单必须显式给出。"""
    config = MinimalPresetConfig()
    with pytest.raises(FrozenInstanceError):
        config.model_name = "changed"  # type: ignore[misc]
    with pytest.raises(ValueError, match="allowed_hosts"):
        ResearchPresetConfig()
    with pytest.raises(ValueError, match="allowed_commands"):
        CodingPresetConfig(allowed_commands=frozenset())
    supplied_environment = {"PATH": "/explicit/bin"}
    coding = CodingPresetConfig(shell_environment=supplied_environment)
    supplied_environment["PATH"] = "/changed"
    assert coding.shell_environment == {"PATH": "/explicit/bin"}
    assert "/explicit/bin" not in repr(coding)
    with pytest.raises(TypeError):
        coding.shell_environment["PATH"] = "/mutation"  # type: ignore[index]
    with pytest.raises(ValueError, match="shell_environment"):
        CodingPresetConfig(shell_environment={"BAD=KEY": "value"})


class _NoopPublisher:
    """同步门面构造测试使用的审计发布器。"""

    async def publish(self, event: LoopEvent) -> None:
        """忽略事件。"""
        del event
