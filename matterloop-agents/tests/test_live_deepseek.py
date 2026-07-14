"""DeepSeek V4 Flash 的显式 opt-in 全闭环真实组合测试。"""

from __future__ import annotations

import asyncio
import json
import os
from collections.abc import Mapping
from dataclasses import replace
from datetime import date

import pytest
from matterloop_agents import (
    AgentDirectory,
    AgentSpec,
    AsyncTeamRuntime,
    CriteriaVerifier,
    CriteriaVerifierConfig,
    InMemoryTeamRepository,
    LeastBusyScheduler,
    LoopAgentEndpoint,
    ModelTaskVerifier,
    ModelTaskVerifierConfig,
    ModelTeamPlanner,
    ModelTeamPlannerConfig,
    ModelTeamReviewer,
    ModelTeamReviewerConfig,
    TeamLimits,
    TeamOrchestrator,
    TeamOrchestratorComponents,
    TeamRequest,
    ToolCallingWorker,
    ToolCallingWorkerConfig,
)
from matterloop_agents.collaboration import (
    AgentEndpoint,
    AgentTaskContext,
    ConcatenateResultAggregator,
    LocalTeamEventPublisher,
    TaskResult,
    TaskSpec,
    TeamEvent,
    TeamEventType,
    TeamPlanningContext,
    TeamStatus,
)
from matterloop_core import (
    AgentLoop,
    ApprovalDecision,
    ComponentRegistry,
    HumanAction,
    HumanInteractionKind,
    HumanResponse,
    LocalEventPublisher,
    LoopContext,
    LoopLimits,
    Plan,
    PlanStep,
)
from matterloop_memory import InMemoryCheckpointStore
from matterloop_models import (
    MessageRole,
    ModelClient,
    ModelMessage,
    ModelRegistry,
    ModelRequest,
    ModelResponse,
    ToolChoice,
)
from matterloop_models.providers import (
    DeepSeekChatModelClient,
    DeepSeekModelConfig,
    DeepSeekReasoningEffort,
    DeepSeekThinkingMode,
)
from matterloop_policies import (
    AllowAllApproval,
    BudgetedAgentEndpoint,
    BudgetedExecutor,
    BudgetedModelClient,
    BudgetedTool,
    BudgetLimits,
    ExponentialBackoffRetryPolicy,
    ResourceLimitExceededError,
    RetryConfig,
    TokenRateCard,
    UsageLedger,
)
from matterloop_runtime import AsyncRuntime
from matterloop_tools import ToolContext, ToolRegistry, ToolResult, ToolSpec

_MODEL_NAME = "deepseek-v4-flash"
_GLOBAL_SCOPE = "live-deepseek"
_LOW_LIMIT_SCOPE = "live-deepseek-low-limit"
_PRICING_EFFECTIVE_DATE_ENV = "DEEPSEEK_PRICING_EFFECTIVE_DATE"
_PRICE_ENV_NAMES = (
    "DEEPSEEK_INPUT_MICROS_PER_MILLION",
    "DEEPSEEK_OUTPUT_MICROS_PER_MILLION",
    "DEEPSEEK_CACHE_HIT_INPUT_MICROS_PER_MILLION",
    "DEEPSEEK_CACHE_MISS_INPUT_MICROS_PER_MILLION",
    "DEEPSEEK_REASONING_OUTPUT_MICROS_PER_MILLION",
)


class _CountingModelClient:
    """只记录真正到达供应商适配器的调用次数。"""

    def __init__(self, client: ModelClient) -> None:
        self._client = client
        self.calls = 0

    async def generate(self, request: ModelRequest) -> ModelResponse:
        """计数后调用已注入模型客户端。"""
        self.calls += 1
        return await self._client.generate(request)


class _RequireInitialToolChoice:
    """强制 Worker 首轮调用证据工具，续轮恢复供应商自动选择。"""

    def __init__(self, client: ModelClient) -> None:
        self._client = client

    async def generate(self, request: ModelRequest) -> ModelResponse:
        """只给带工具且尚无工具输出的请求设置 REQUIRED。"""
        if request.tools and not request.tool_outputs and request.continuation is None:
            request = replace(request, tool_choice=ToolChoice.REQUIRED)
        return await self._client.generate(request)


class _InMemoryEvidenceTool:
    """返回确定性内存证据，不访问文件、进程或网络。"""

    spec = ToolSpec(
        name="memory_evidence",
        description="从当前进程内的固定证据集中返回一条可引用证据",
        input_schema={
            "type": "object",
            "additionalProperties": False,
            "properties": {"claim": {"type": "string", "minLength": 1}},
            "required": ["claim"],
        },
    )

    async def invoke(
        self,
        arguments: Mapping[str, object],
        context: ToolContext,
    ) -> ToolResult:
        """把模型声明与稳定来源标识包装成 JSON 文本。"""
        claim = arguments.get("claim")
        normalized_claim = claim if isinstance(claim, str) and claim.strip() else "未命名声明"
        return ToolResult(
            content=json.dumps(
                {
                    "source": "in-memory-evidence",
                    "verified": True,
                    "claim": normalized_claim,
                    "run_id": context.run_id,
                },
                ensure_ascii=False,
            )
        )


class _EvidenceLoopPlanner:
    """为每个团队任务生成一个必须调用内存证据工具的 Core 计划。"""

    async def plan(self, context: LoopContext) -> Plan:
        """返回单步骤、无人工审批且使用默认执行器的计划。"""
        criteria = context.request.acceptance_criteria or (
            "最终输出必须引用 source=in-memory-evidence 且 verified=true 的工具证据",
        )
        return Plan(
            steps=(
                PlanStep(
                    description=(
                        f"{context.request.goal}\n"
                        "必须调用 memory_evidence 工具获取证据；不得跳过工具，"
                        "并在最终回答中明确引用工具返回的 source 和 verified 字段。"
                    ),
                    executor="default",
                    acceptance_criteria=criteria,
                    requires_approval=False,
                    step_id="collect-memory-evidence",
                ),
            )
        )


class _AlwaysContinuePolicy:
    """把资源限制交给所有预算代理原子强制执行。"""

    def can_continue(self, context: LoopContext) -> bool:
        """允许内核推进到下一个安全边界。"""
        del context
        return True


class _DeferredTeamApprovalGate:
    """把首个显式审批任务转换成人工交互。"""

    def __init__(self) -> None:
        self.calls = 0

    async def decide(self, context: AgentTaskContext) -> ApprovalDecision:
        """记录审批并固定返回延期。"""
        del context
        self.calls += 1
        return ApprovalDecision.DEFERRED


class _ApprovalThenModelPlanner:
    """首轮产生审批任务，人工修订后委托真实模型生成两个并行任务。"""

    def __init__(self, delegate: ModelTeamPlanner) -> None:
        self._delegate = delegate

    async def plan(self, context: TeamPlanningContext) -> tuple[TaskSpec, ...]:
        """按 plan revision 选择确定性审批计划或模型计划。"""
        if context.plan_revision == 0:
            return (
                TaskSpec(
                    task_id="human-approval-before-live-run",
                    description="在产生真实模型费用前请求人类检查并修订团队计划",
                    capability="evidence-a",
                    acceptance_criteria=("获得明确人工反馈",),
                    requires_approval=True,
                    priority=100,
                ),
            )

        tasks = await self._delegate.plan(context)
        if len(tasks) != 2:
            raise RuntimeError("live TeamPlanner must return exactly two tasks")
        if {task.capability for task in tasks} != {"evidence-a", "evidence-b"}:
            raise RuntimeError("live TeamPlanner must use both registered evidence capabilities")
        if any(task.dependencies for task in tasks):
            raise RuntimeError("live TeamPlanner tasks must be independent")
        if any(task.requires_approval for task in tasks):
            raise RuntimeError("revised live TeamPlanner tasks must not request another approval")
        if any("memory_evidence" not in task.description for task in tasks):
            raise RuntimeError("live TeamPlanner task descriptions must require memory_evidence")
        return tasks


class _ConcurrencyProbe:
    """记录两个 Agent 端点是否实际发生执行重叠。"""

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self.active = 0
        self.max_active = 0

    async def enter(self) -> None:
        """增加活跃端点计数。"""
        async with self._lock:
            self.active += 1
            self.max_active = max(self.max_active, self.active)

    async def leave(self) -> None:
        """减少活跃端点计数。"""
        async with self._lock:
            self.active -= 1


class _ProbedEndpoint:
    """在真实 LoopAgentEndpoint 外增加并行度探针。"""

    def __init__(self, endpoint: AgentEndpoint, probe: _ConcurrencyProbe) -> None:
        self._endpoint = endpoint
        self._probe = probe

    @property
    def spec(self) -> AgentSpec:
        """原样暴露被代理端点的 Agent 规范。"""
        return self._endpoint.spec

    async def execute(self, context: AgentTaskContext) -> TaskResult:
        """记录端点活跃区间并执行完整子 Loop。"""
        await self._probe.enter()
        try:
            return await self._endpoint.execute(context)
        finally:
            await self._probe.leave()


def _read_rate_card() -> TokenRateCard:
    """从测试组合根的显式环境变量读取价格表。"""
    required_names = (*_PRICE_ENV_NAMES, _PRICING_EFFECTIVE_DATE_ENV)
    missing = tuple(name for name in required_names if not os.environ.get(name, "").strip())
    if missing:
        pytest.skip("DeepSeek live pricing environment variables are not configured")
    values: dict[str, int] = {}
    for name in _PRICE_ENV_NAMES:
        raw_value = os.environ[name].strip()
        try:
            value = int(raw_value)
        except ValueError:
            pytest.fail(f"{name} must be an integer micro-USD rate")
        if value < 0:
            pytest.fail(f"{name} must not be negative")
        values[name] = value
    raw_effective_date = os.environ[_PRICING_EFFECTIVE_DATE_ENV].strip()
    try:
        effective_from = date.fromisoformat(raw_effective_date)
    except ValueError:
        pytest.fail(f"{_PRICING_EFFECTIVE_DATE_ENV} must use YYYY-MM-DD format")
    return TokenRateCard(
        currency="USD",
        effective_from=effective_from,
        input_micros_per_million=values["DEEPSEEK_INPUT_MICROS_PER_MILLION"],
        output_micros_per_million=values["DEEPSEEK_OUTPUT_MICROS_PER_MILLION"],
        cache_hit_input_micros_per_million=values["DEEPSEEK_CACHE_HIT_INPUT_MICROS_PER_MILLION"],
        cache_miss_input_micros_per_million=values["DEEPSEEK_CACHE_MISS_INPUT_MICROS_PER_MILLION"],
        reasoning_output_micros_per_million=values["DEEPSEEK_REASONING_OUTPUT_MICROS_PER_MILLION"],
    )


def _loop_usage_scopes(context: LoopContext) -> tuple[str, ...]:
    """解析子 Loop 继承的多层额度作用域。"""
    raw = context.request.metadata.get("usage_scopes", ())
    inherited = (
        tuple(item for item in raw if isinstance(item, str) and item.strip())
        if isinstance(raw, (tuple, list))
        else ()
    )
    return tuple(dict.fromkeys((*inherited, context.run_id)))


def _tool_usage_scopes(context: ToolContext) -> tuple[str, ...]:
    """让安全工具调用同时归集到全局与子 Loop。"""
    return (_GLOBAL_SCOPE, context.run_id)


def _agent_usage_scopes(context: AgentTaskContext) -> tuple[str, ...]:
    """让 Agent 任务同时归集到团队、任务、Agent 与全局。"""
    return (
        _GLOBAL_SCOPE,
        f"team:{context.team_run_id}",
        f"task:{context.team_run_id}:{context.task.task_id}",
        f"agent:{context.agent_id}",
    )


def _build_child_runtime(models: ModelRegistry, ledger: UsageLedger) -> AsyncRuntime:
    """装配单步骤证据工具 Core Loop。"""
    tools = ToolRegistry(
        [
            BudgetedTool(
                _InMemoryEvidenceTool(),
                ledger,
                scope_resolver=_tool_usage_scopes,
            )
        ]
    )
    worker = ToolCallingWorker(
        models,
        tools,
        ToolCallingWorkerConfig(
            model="deepseek",
            tool_names=("memory_evidence",),
            max_tool_rounds=1,
            max_output_tokens=1024,
        ),
    )
    planners = ComponentRegistry()
    executors = ComponentRegistry()
    verifiers = ComponentRegistry()
    planners.register("default", _EvidenceLoopPlanner())
    executors.register(
        "default",
        BudgetedExecutor(worker, ledger, scope_resolver=_loop_usage_scopes),
    )
    verifiers.register(
        "default",
        CriteriaVerifier(
            models,
            CriteriaVerifierConfig(
                model="deepseek",
                pass_score=50,
                max_output_tokens=768,
            ),
        ),
    )
    loop = AgentLoop(
        planners,
        executors,
        verifiers,
        InMemoryCheckpointStore(),
        _AlwaysContinuePolicy(),
        LocalEventPublisher(),
        AllowAllApproval(),
        ExponentialBackoffRetryPolicy(
            RetryConfig(
                max_attempts=1,
                base_delay_seconds=0,
                max_delay_seconds=0,
                jitter_ratio=0,
            ),
            retryable=(),
        ),
    )
    return AsyncRuntime(loop, resources=(tools,))


def _register_loop_endpoint(
    directory: AgentDirectory,
    runtime: AsyncRuntime,
    *,
    agent_id: str,
    capability: str,
    ledger: UsageLedger,
    probe: _ConcurrencyProbe,
) -> None:
    """注册带 Agent 任务额度和并发探针的 LoopAgentEndpoint。"""
    endpoint = LoopAgentEndpoint(
        AgentSpec(
            agent_id=agent_id,
            capabilities=frozenset({capability}),
            max_concurrency=1,
            role="evidence-worker",
            description="运行完整 Core Loop 并调用内存证据工具",
        ),
        runtime,
        limits=LoopLimits(
            max_cycles=1,
            max_attempts=1,
            max_steps_per_plan=1,
            timeout_seconds=180,
        ),
    )
    budgeted = BudgetedAgentEndpoint(
        endpoint,
        ledger,
        scope_resolver=_agent_usage_scopes,
    )
    directory.register(_ProbedEndpoint(budgeted, probe))


@pytest.mark.live_deepseek
def test_live_deepseek_human_feedback_team_loop_and_budget() -> None:
    """真实验证人工修订、并行子 Loop、工具续轮、审查和本地额度。"""
    if os.environ.get("MATTERLOOP_RUN_LIVE_DEEPSEEK") != "1":
        pytest.skip("set MATTERLOOP_RUN_LIVE_DEEPSEEK=1 to enable paid live testing")
    api_key = os.environ.get("DEEPSEEK_API_KEY", "").strip()
    if not api_key:
        pytest.skip("DEEPSEEK_API_KEY is not configured")
    rate_card = _read_rate_card()
    openai = pytest.importorskip("openai")
    sdk_client = openai.AsyncOpenAI(
        api_key=api_key,
        base_url="https://api.deepseek.com",
        timeout=60,
        max_retries=0,
    )
    del api_key

    asyncio.run(_run_live_scenario(sdk_client, rate_card))


async def _run_live_scenario(sdk_client: object, rate_card: TokenRateCard) -> None:
    """运行付费场景，并保证所有异步资源最终释放。"""
    deepseek = DeepSeekChatModelClient(
        DeepSeekModelConfig(
            model=_MODEL_NAME,
            thinking_mode=DeepSeekThinkingMode.ENABLED,
            reasoning_effort=DeepSeekReasoningEffort.HIGH,
        ),
        client=sdk_client,  # type: ignore[arg-type]
    )
    limits = BudgetLimits(
        max_model_calls=12,
        max_concurrent_model_calls=2,
        max_total_tokens=40_000,
        max_cost_micros=30_000,
        cost_currency="USD",
        max_tool_calls=6,
        max_agent_tasks=2,
        max_attempts=2,
    )
    ledger = UsageLedger(default_limits=limits)
    full_counter = _CountingModelClient(deepseek)
    full_budgeted = BudgetedModelClient(
        full_counter,
        ledger,
        rate_card=rate_card,
        default_scopes=(_GLOBAL_SCOPE,),
        default_max_output_tokens=1024,
    )
    models = ModelRegistry()
    models.register("deepseek", _RequireInitialToolChoice(full_budgeted))

    runtime_a = _build_child_runtime(models, ledger)
    runtime_b = _build_child_runtime(models, ledger)
    directory = AgentDirectory()
    probe = _ConcurrencyProbe()
    _register_loop_endpoint(
        directory,
        runtime_a,
        agent_id="evidence-agent-a",
        capability="evidence-a",
        ledger=ledger,
        probe=probe,
    )
    _register_loop_endpoint(
        directory,
        runtime_b,
        agent_id="evidence-agent-b",
        capability="evidence-b",
        ledger=ledger,
        probe=probe,
    )

    event_types: list[str] = []
    events = LocalTeamEventPublisher()

    def record_event(event: TeamEvent) -> None:
        event_types.append(event.event_type.value)

    events.subscribe(record_event)
    approval_gate = _DeferredTeamApprovalGate()
    model_planner = ModelTeamPlanner(
        models,
        ModelTeamPlannerConfig(
            model="deepseek",
            max_tasks=2,
            max_output_tokens=1024,
        ),
    )
    orchestrator = TeamOrchestrator(
        TeamOrchestratorComponents(
            planner=_ApprovalThenModelPlanner(model_planner),
            agents=directory,
            selection_policy=LeastBusyScheduler(),
            verifier=ModelTaskVerifier(
                models,
                ModelTaskVerifierConfig(
                    model="deepseek",
                    pass_score=50,
                    max_output_tokens=768,
                ),
            ),
            approval_gate=approval_gate,
            repository=InMemoryTeamRepository(),
            events=events,
            aggregator=ConcatenateResultAggregator(),
            reviewer=ModelTeamReviewer(
                models,
                ModelTeamReviewerConfig(
                    model="deepseek",
                    pass_score=50,
                    max_output_tokens=768,
                ),
            ),
        )
    )
    team_runtime = AsyncTeamRuntime(
        orchestrator,
        resources=(runtime_a, runtime_b),
    )

    try:
        request = TeamRequest(
            goal=(
                "完成 DeepSeek 全闭环真实测试。收到人工 REVISE 后，必须生成恰好两个"
                "无依赖并行任务：capability 分别为 evidence-a 和 evidence-b，"
                "requires_approval 均为 false；每个任务描述必须明确要求其子 Core Loop "
                "调用 memory_evidence 工具并基于返回证据完成。"
            ),
            acceptance_criteria=(
                "两个能力各完成一个经过独立验证的任务",
                "每个任务结果都引用 source=in-memory-evidence 且 verified=true",
            ),
            limits=TeamLimits(
                max_tasks=2,
                max_concurrency=2,
                max_task_attempts=1,
                max_cycles=3,
                max_plan_revisions=2,
                timeout_seconds=300,
            ),
            metadata={"usage_scopes": (_GLOBAL_SCOPE,)},
        )
        paused = await team_runtime.run(request, run_id="live-deepseek-team")
        assert paused.status is TeamStatus.PAUSED
        assert paused.pending_interaction is not None
        assert paused.pending_interaction.kind is HumanInteractionKind.APPROVAL
        assert approval_gate.calls == 1
        assert full_counter.calls == 0

        interaction_id = paused.pending_interaction.interaction_id
        submitted = await team_runtime.submit_human_response(
            paused.run_id,
            HumanResponse(
                interaction_id=interaction_id,
                action=HumanAction.REVISE,
                content=(
                    "请重新规划为恰好两个无依赖并行任务：分别使用 evidence-a 和 "
                    "evidence-b，均无需再次审批；每个任务描述必须要求调用 "
                    "memory_evidence 并引用工具证据。"
                ),
                idempotency_key="live-deepseek-revise-1",
            ),
        )
        assert submitted.status is TeamStatus.PAUSED
        assert submitted.pending_interaction is None
        assert submitted.human_interactions[-1].response.action is HumanAction.REVISE
        assert full_counter.calls == 0

        completed = await team_runtime.resume(paused.run_id)
        assert completed.status is TeamStatus.COMPLETED
        assert completed.completed_tasks == 2
        assert completed.cycle == 2
        assert probe.max_active == 2
        assert all(
            result.metadata.get("loop_status") == "completed" for result in completed.task_results
        )
        assert all(
            result.metadata.get("loop_completed_steps") == 1 for result in completed.task_results
        )

        full_usage = ledger.snapshot(_GLOBAL_SCOPE)
        assert full_usage.model_calls == full_counter.calls
        assert full_usage.model_calls <= 11
        assert full_usage.active_model_calls == 0
        assert full_usage.total_tokens <= 40_000
        assert full_usage.cost_for("USD") <= 30_000
        assert 2 <= full_usage.tool_calls <= 6
        assert full_usage.agent_tasks == 2
        assert full_usage.attempts == 2
        assert event_types.count(TeamEventType.TASK_STARTED.value) == 2
        assert TeamEventType.HUMAN_INTERACTION_REQUESTED.value in event_types
        assert TeamEventType.HUMAN_REVISED.value in event_types
        assert TeamEventType.TEAM_RESUMED.value in event_types
        assert TeamEventType.REVIEW_STARTED.value in event_types
        assert TeamEventType.REVIEW_COMPLETED.value in event_types
        assert TeamEventType.TEAM_COMPLETED.value in event_types

        ledger.configure_scope(
            _LOW_LIMIT_SCOPE,
            BudgetLimits(
                max_model_calls=1,
                max_concurrent_model_calls=1,
                max_total_tokens=4096,
                max_cost_micros=30_000,
                cost_currency="USD",
            ),
        )
        low_counter = _CountingModelClient(deepseek)
        low_budgeted = BudgetedModelClient(
            low_counter,
            ledger,
            rate_card=rate_card,
            default_scopes=(_GLOBAL_SCOPE, _LOW_LIMIT_SCOPE),
            default_max_output_tokens=64,
        )
        low_request = ModelRequest(
            messages=(ModelMessage(MessageRole.USER, "只回复 OK"),),
            max_output_tokens=64,
            usage_scopes=(_GLOBAL_SCOPE, _LOW_LIMIT_SCOPE),
        )
        await low_budgeted.generate(low_request)
        calls_before_rejection = ledger.snapshot(_GLOBAL_SCOPE).model_calls

        with pytest.raises(ResourceLimitExceededError) as captured:
            await low_budgeted.generate(low_request)

        assert captured.value.resource == "model_calls"
        assert low_counter.calls == 1
        final_usage = ledger.snapshot(_GLOBAL_SCOPE)
        assert final_usage.model_calls == calls_before_rejection
        assert final_usage.model_calls == full_counter.calls + 1
        assert final_usage.model_calls <= 12
        assert final_usage.total_tokens <= 40_000
        assert final_usage.cost_for("USD") <= 30_000

        print(
            "live_deepseek_summary="
            + json.dumps(
                {
                    "status": completed.status.value,
                    "event_types": event_types,
                    "model_calls": final_usage.model_calls,
                    "tool_calls": final_usage.tool_calls,
                    "agent_tasks": final_usage.agent_tasks,
                    "total_tokens": final_usage.total_tokens,
                    "cost_micros_usd": final_usage.cost_for("USD"),
                },
                ensure_ascii=False,
                sort_keys=True,
            )
        )
    finally:
        try:
            await team_runtime.aclose()
        finally:
            close = getattr(sdk_client, "close", None)
            if close is not None:
                await close()
