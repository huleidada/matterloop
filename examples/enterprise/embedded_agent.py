"""演示嵌入式单 Agent 的人工修订、工具、记忆、预算和审计闭环。"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Mapping
from dataclasses import dataclass

from matterloop_agents import (
    CriteriaVerifier,
    CriteriaVerifierConfig,
    ModelPlanner,
    ModelPlannerConfig,
    ToolCallingWorker,
    ToolCallingWorkerConfig,
)
from matterloop_core import (
    AgentLoop,
    ApprovalDecision,
    ComponentRegistry,
    Executor,
    HumanAction,
    HumanResponse,
    LoopEvent,
    LoopPolicy,
    LoopRequest,
    LoopStatus,
    Planner,
    RetryPolicy,
    Verifier,
)
from matterloop_memory import (
    InMemoryCheckpointStore,
    InMemoryMemoryStore,
    MemoryKind,
    MemoryRecord,
)
from matterloop_models import (
    FakeModelClient,
    ModelRegistry,
    ModelResponse,
    TokenUsage,
    ToolCall,
)
from matterloop_observability import (
    CompositeEventPublisher,
    HandlerEventPublisher,
    PublisherFailureMode,
)
from matterloop_policies import (
    BudgetedModelClient,
    BudgetedTool,
    BudgetLimits,
    BudgetPolicy,
    CompositeLoopPolicy,
    ExponentialBackoffRetryPolicy,
    NoProgressStopPolicy,
    RetryConfig,
    UsageLedger,
)
from matterloop_runtime import AsyncRuntime
from matterloop_tools import ToolContext, ToolRegistry, ToolResult, ToolSpec

logger = logging.getLogger(__name__)


class DeferredApprovalGate:
    """把显式标记的高风险步骤交给外部人工反馈 API。"""

    async def decide(self, step: object, context: object) -> ApprovalDecision:
        """返回延迟审批决策。

        Args:
            step: 当前步骤；示例不根据步骤内容自动放行。
            context: 当前运行上下文；示例不从上下文推断审批结果。

        Returns:
            要求调用方提交结构化人工响应的决策。
        """
        del step, context
        return ApprovalDecision.DEFERRED


class EvidenceTool:
    """返回无副作用的内存证据，便于演示受预算保护的工具调用。"""

    spec = ToolSpec(
        name="evidence.lookup",
        description="读取示例内置的审核证据",
        input_schema={
            "type": "object",
            "properties": {"topic": {"type": "string"}},
            "required": ["topic"],
            "additionalProperties": False,
        },
    )

    async def invoke(
        self,
        arguments: Mapping[str, object],
        context: ToolContext,
    ) -> ToolResult:
        """根据主题返回确定性证据。

        Args:
            arguments: 必须包含字符串 `topic` 的工具参数。
            context: 提供 run_id 和 step_id 的工具调用上下文。

        Returns:
            不包含凭据或外部数据的文本证据。

        Raises:
            ValueError: `topic` 不是非空字符串。
        """
        topic = arguments.get("topic")
        if not isinstance(topic, str) or not topic.strip():
            raise ValueError("topic must be a non-empty string")
        return ToolResult(
            content=f"{topic.strip()}：离线证据已核验",
            metadata={"run_id": context.run_id, "source": "in-memory"},
        )


class RecordingEventHandler:
    """只保存非敏感事件类型和序号的示例审计处理器。"""

    def __init__(self) -> None:
        self.events: list[tuple[str, int]] = []

    def __call__(self, event: LoopEvent) -> None:
        """记录事件稳定标识。

        Args:
            event: Core 发布的隔离生命周期事件。
        """
        self.events.append((event.event_type.value, event.sequence))


@dataclass(frozen=True, slots=True)
class EmbeddedExampleResult:
    """嵌入式示例的可断言结果摘要。"""

    run_id: str
    status: LoopStatus
    output: str
    cycles: int
    model_calls: int
    tool_calls: int
    event_names: tuple[str, ...]
    human_feedback: tuple[str, ...]


async def run_embedded_example() -> EmbeddedExampleResult:
    """执行一次暂停、人工修订、重规划和验收的完整闭环。

    Returns:
        仅包含状态、计量和事件名称的非敏感摘要。
    """
    ledger = UsageLedger(
        BudgetLimits(
            max_model_calls=6,
            max_concurrent_model_calls=1,
            max_total_tokens=300_000,
            max_tool_calls=2,
        )
    )
    models = _build_models(ledger)
    memory = InMemoryMemoryStore()
    await memory.put(
        MemoryRecord(
            namespace="enterprise-demo",
            kind=MemoryKind.PROCEDURAL,
            content="高风险变更必须先经过人工确认，并保留验收证据。",
        )
    )

    evidence_tool = BudgetedTool(EvidenceTool(), ledger)
    tools = ToolRegistry((evidence_tool,))
    planner = ModelPlanner(
        models,
        ModelPlannerConfig(model="planner", memory_namespace="enterprise-demo"),
        memory=memory,
    )
    worker = ToolCallingWorker(
        models,
        tools,
        ToolCallingWorkerConfig(model="worker", tool_names=("evidence.lookup",)),
    )
    verifier = CriteriaVerifier(models, CriteriaVerifierConfig(model="verifier"))

    planners = ComponentRegistry[Planner]()
    executors = ComponentRegistry[Executor]()
    verifiers = ComponentRegistry[Verifier]()
    planners.register("default", planner)
    executors.register("default", worker)
    verifiers.register("default", verifier)

    audit = RecordingEventHandler()
    publisher = CompositeEventPublisher(
        (HandlerEventPublisher(audit),),
        failure_mode=PublisherFailureMode.RAISE,
    )
    loop_policy: LoopPolicy = CompositeLoopPolicy(
        BudgetPolicy(
            BudgetLimits(max_model_calls=6, max_total_tokens=300_000, max_tool_calls=2),
            ledger,
        ),
        NoProgressStopPolicy(),
    )
    retry_policy: RetryPolicy = ExponentialBackoffRetryPolicy(
        RetryConfig(max_attempts=1, base_delay_seconds=0, max_delay_seconds=0, jitter_ratio=0)
    )
    loop = AgentLoop(
        planners=planners,
        executors=executors,
        verifiers=verifiers,
        checkpoint_store=InMemoryCheckpointStore(),
        policy=loop_policy,
        events=publisher,
        approval_gate=DeferredApprovalGate(),
        retry_policy=retry_policy,
    )

    runtime = AsyncRuntime(loop, resources=(tools,))
    async with runtime:
        run_id = runtime.create_run_id()
        request = LoopRequest(
            goal="生成经过证据核验的企业变更说明",
            acceptance_criteria=("包含离线证据", "通过独立验证"),
            metadata={"usage_scopes": ("tenant:demo", run_id)},
        )
        paused = await runtime.run(request, run_id=run_id)
        if paused.status is not LoopStatus.PAUSED or paused.pending_interaction is None:
            raise RuntimeError("embedded example did not reach the expected human pause")

        await runtime.submit_human_response(
            run_id,
            HumanResponse(
                interaction_id=paused.pending_interaction.interaction_id,
                action=HumanAction.REVISE,
                content="删除写操作，只保留离线证据核验。",
                idempotency_key="embedded-revision-1",
            ),
        )
        completed = await runtime.resume(run_id)

    usage = ledger.snapshot(run_id)
    return EmbeddedExampleResult(
        run_id=run_id,
        status=completed.status,
        output=completed.output,
        cycles=completed.cycles,
        model_calls=usage.model_calls,
        tool_calls=usage.tool_calls,
        event_names=tuple(name for name, _ in audit.events),
        human_feedback=tuple(record.response.content for record in completed.human_interactions),
    )


def _build_models(ledger: UsageLedger) -> ModelRegistry:
    """创建具有独立职责和共享额度账本的三个假模型。"""
    planner_responses = (
        ModelResponse(
            output_text=(
                '{"steps":[{"description":"执行高风险写入",'
                '"executor":"default","acceptance_criteria":["人工批准"],'
                '"requires_approval":true}]}'
            ),
            usage=TokenUsage(input_tokens=20, output_tokens=15, total_tokens=35),
        ),
        ModelResponse(
            output_text=(
                '{"steps":[{"description":"核验只读证据",'
                '"executor":"default","acceptance_criteria":["包含离线证据"],'
                '"requires_approval":false}]}'
            ),
            usage=TokenUsage(input_tokens=24, output_tokens=16, total_tokens=40),
        ),
    )
    worker_responses = (
        ModelResponse(
            tool_calls=(
                ToolCall(
                    call_id="evidence-call-1",
                    name="evidence.lookup",
                    arguments={"topic": "变更说明"},
                ),
            ),
            response_id="worker-response-1",
            usage=TokenUsage(input_tokens=18, output_tokens=8, total_tokens=26),
        ),
        ModelResponse(
            output_text="变更说明已包含离线证据，并且未执行任何外部写操作。",
            usage=TokenUsage(input_tokens=26, output_tokens=12, total_tokens=38),
        ),
    )
    verifier_responses = (
        ModelResponse(
            output_text=(
                '{"passed":true,"score":100,"feedback":"验收通过",'
                '"evidence":["离线证据已核验"],"failed_criteria":[]}'
            ),
            usage=TokenUsage(input_tokens=22, output_tokens=12, total_tokens=34),
        ),
    )

    registry = ModelRegistry()
    registry.register(
        "planner",
        BudgetedModelClient(
            FakeModelClient(planner_responses),
            ledger,
            default_max_output_tokens=256,
        ),
    )
    registry.register(
        "worker",
        BudgetedModelClient(
            FakeModelClient(worker_responses),
            ledger,
            default_max_output_tokens=256,
        ),
    )
    registry.register(
        "verifier",
        BudgetedModelClient(
            FakeModelClient(verifier_responses),
            ledger,
            default_max_output_tokens=256,
        ),
    )
    return registry


def main() -> None:
    """运行示例并输出不含提示词、凭据和推理内容的摘要。"""
    logging.basicConfig(level=logging.INFO)
    result = asyncio.run(run_embedded_example())
    logger.info(
        "嵌入式示例完成",
        extra={
            "run_id": result.run_id,
            "status": result.status.value,
            "cycles": result.cycles,
            "model_calls": result.model_calls,
            "tool_calls": result.tool_calls,
        },
    )


if __name__ == "__main__":
    main()
