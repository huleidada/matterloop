"""原子计算额度、费率表与结构代理测试。"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date

import pytest
from matterloop_core import ExecutionResult, LoopContext, LoopRequest, PlanStep
from matterloop_models import (
    MessageRole,
    ModelContinuation,
    ModelMessage,
    ModelRequest,
    ModelResponse,
    ModelResponseParseError,
    TokenUsage,
)
from matterloop_policies import (
    BudgetConfigurationError,
    BudgetedAgentEndpoint,
    BudgetedExecutor,
    BudgetedModelClient,
    BudgetedTool,
    BudgetLimits,
    ResourceLimitExceededError,
    TokenRateCard,
    UsageAmount,
    UsageLedger,
    UsageReservationError,
)
from matterloop_tools import ToolContext, ToolResult, ToolSpec


def test_reservation_is_atomic_across_multiple_scopes() -> None:
    """任一子 scope 超限时父 scope 不得留下部分预留。"""
    ledger = UsageLedger(BudgetLimits(max_agent_tasks=2))
    ledger.configure_scope("task:one", BudgetLimits(max_agent_tasks=1))
    first = ledger.reserve(("team", "task:one"), UsageAmount(agent_tasks=1))

    with pytest.raises(ResourceLimitExceededError) as captured:
        ledger.reserve(("team", "task:one"), UsageAmount(agent_tasks=1))

    assert captured.value.scope == "task:one"
    assert ledger.snapshot("team").reserved.agent_tasks == 1
    ledger.commit(first)
    assert ledger.snapshot("team").agent_tasks == 1
    assert ledger.snapshot("task:one").agent_tasks == 1


def test_rollback_releases_concurrency_and_all_reserved_dimensions() -> None:
    """失败调用回滚后必须允许后续调用重新申请全部额度。"""
    ledger = UsageLedger(
        BudgetLimits(max_model_calls=1, max_concurrent_model_calls=1, max_total_tokens=10)
    )
    amount = UsageAmount(model_calls=1, input_tokens=4, output_tokens=6, total_tokens=10)
    reservation = ledger.reserve("run", amount)

    assert ledger.snapshot("run").active_model_calls == 1
    with pytest.raises(ResourceLimitExceededError):
        ledger.reserve("run", amount)

    ledger.rollback(reservation)
    replacement = ledger.reserve("run", amount)
    ledger.commit(replacement, UsageAmount(model_calls=1, total_tokens=4))
    snapshot = ledger.snapshot("run")
    assert snapshot.model_calls == 1
    assert snapshot.total_tokens == 4
    assert snapshot.active_model_calls == 0
    assert snapshot.reserved.is_zero


def test_duplicate_reservation_finalization_is_rejected() -> None:
    """同一预留不能被重复结算或回滚。"""
    ledger = UsageLedger()
    reservation = ledger.reserve("run", UsageAmount(tool_calls=1))
    ledger.commit(reservation)

    with pytest.raises(UsageReservationError):
        ledger.rollback(reservation)


def test_usage_snapshot_keeps_currencies_and_token_details_separate() -> None:
    """费用按币种记录，缓存和 reasoning 仅作为 Token 明细。"""
    ledger = UsageLedger()
    ledger.record_model_usage(
        ("team", "agent"),
        input_tokens=12,
        output_tokens=8,
        cache_hit_tokens=7,
        cache_miss_tokens=5,
        reasoning_tokens=3,
        cost_micros=9,
        currency="cny",
    )
    ledger.consume(("team", "agent"), UsageAmount(costs_micros={"USD": 4}))

    snapshot = ledger.snapshot("team")
    assert snapshot.total_tokens == 20
    assert snapshot.cache_hit_tokens == 7
    assert snapshot.cache_miss_tokens == 5
    assert snapshot.reasoning_tokens == 3
    assert snapshot.cost_for("CNY") == 9
    assert snapshot.cost_micros == 4
    assert ledger.snapshot("agent") == snapshot


def test_rate_card_does_not_double_count_cache_or_reasoning_details() -> None:
    """缓存和 reasoning 明细应替代基础费率，而不是叠加计费。"""
    card = TokenRateCard(
        currency="usd",
        effective_from=date(2026, 1, 1),
        input_micros_per_million=1_000_000,
        output_micros_per_million=2_000_000,
        cache_hit_input_micros_per_million=500_000,
        cache_miss_input_micros_per_million=1_500_000,
        reasoning_output_micros_per_million=3_000_000,
    )
    usage = TokenUsage(
        input_tokens=100,
        output_tokens=40,
        total_tokens=140,
        cache_hit_tokens=60,
        cache_miss_tokens=40,
        reasoning_tokens=10,
    )

    assert card.currency == "USD"
    assert card.calculate_cost(usage) == 180
    assert card.estimate_max_cost(input_tokens=100, output_tokens=40) == 270


class _BlockingModelClient:
    """让测试能够观察模型调用进行中的并发预留。"""

    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def generate(self, request: ModelRequest) -> ModelResponse:
        del request
        self.started.set()
        await self.release.wait()
        return ModelResponse(usage=TokenUsage(input_tokens=3, output_tokens=2, total_tokens=5))


def test_budgeted_model_client_blocks_concurrent_oversell() -> None:
    """两个并发模型调用不能共同穿透单并发硬上限。"""

    async def scenario() -> None:
        ledger = UsageLedger(
            BudgetLimits(
                max_model_calls=2,
                max_concurrent_model_calls=1,
                max_input_tokens=20,
                max_output_tokens=20,
                max_total_tokens=40,
            )
        )
        delegate = _BlockingModelClient()
        client = BudgetedModelClient(
            delegate,
            ledger,
            default_max_output_tokens=5,
            input_token_estimator=lambda request: 5,
        )
        request = ModelRequest(
            (ModelMessage(MessageRole.USER, "test"),),
            max_output_tokens=5,
            usage_scopes=("team",),
        )
        first = asyncio.create_task(client.generate(request))
        await delegate.started.wait()

        with pytest.raises(ResourceLimitExceededError) as captured:
            await client.generate(request)

        assert captured.value.resource == "concurrent_model_calls"
        assert ledger.snapshot("team").active_model_calls == 1
        delegate.release.set()
        await first
        snapshot = ledger.snapshot("team")
        assert snapshot.active_model_calls == 0
        assert snapshot.model_calls == 1
        assert snapshot.total_tokens == 5

    asyncio.run(scenario())


class _FailingModelClient:
    async def generate(self, request: ModelRequest) -> ModelResponse:
        del request
        raise RuntimeError("provider failure")


def test_budgeted_model_client_rolls_back_provider_failure() -> None:
    """供应商异常不得消耗模型调用或遗留并发占位。"""

    async def scenario() -> None:
        ledger = UsageLedger(BudgetLimits(max_model_calls=1))
        client = BudgetedModelClient(
            _FailingModelClient(),
            ledger,
            input_token_estimator=lambda request: 1,
        )
        request = ModelRequest(
            (ModelMessage(MessageRole.USER, "test"),),
            max_output_tokens=1,
            usage_scopes=("run",),
        )

        with pytest.raises(RuntimeError, match="provider failure"):
            await client.generate(request)

        assert ledger.snapshot("run").model_calls == 0
        assert ledger.snapshot("run").active_model_calls == 0
        assert ledger.snapshot("run").reserved.is_zero

    asyncio.run(scenario())


class _ParseFailingModelClient:
    """模拟远端已返回但适配器无法归一化响应的场景。"""

    def __init__(self, error: ModelResponseParseError) -> None:
        self._error = error

    async def generate(self, request: ModelRequest) -> ModelResponse:
        del request
        raise self._error


def test_budgeted_model_client_commits_usage_from_parse_error() -> None:
    """解析失败携带实际 usage 时仍必须结算调用、Token 与费用。"""

    async def scenario() -> None:
        usage = TokenUsage(
            input_tokens=10,
            output_tokens=4,
            total_tokens=14,
            cache_hit_tokens=6,
            cache_miss_tokens=4,
            reasoning_tokens=2,
        )
        error = ModelResponseParseError("safe parse failure", usage=usage)
        ledger = UsageLedger(BudgetLimits(max_model_calls=2, max_total_tokens=30))
        rate_card = TokenRateCard(
            currency="USD",
            effective_from=date(2026, 1, 1),
            input_micros_per_million=2_000_000,
            output_micros_per_million=3_000_000,
            cache_hit_input_micros_per_million=1_000_000,
            cache_miss_input_micros_per_million=2_000_000,
            reasoning_output_micros_per_million=4_000_000,
        )
        client = BudgetedModelClient(
            _ParseFailingModelClient(error),
            ledger,
            rate_card=rate_card,
            input_token_estimator=lambda request: 10,
        )
        request = ModelRequest(
            (ModelMessage(MessageRole.USER, "test"),),
            max_output_tokens=4,
            usage_scopes=("team", "task"),
        )

        with pytest.raises(ModelResponseParseError) as captured:
            await client.generate(request)

        assert captured.value is error
        for scope in ("team", "task"):
            snapshot = ledger.snapshot(scope)
            assert snapshot.model_calls == 1
            assert snapshot.input_tokens == 10
            assert snapshot.output_tokens == 4
            assert snapshot.total_tokens == 14
            assert snapshot.cache_hit_tokens == 6
            assert snapshot.cache_miss_tokens == 4
            assert snapshot.reasoning_tokens == 2
            assert snapshot.cost_micros == 28
            assert snapshot.active_model_calls == 0
            assert snapshot.reserved.is_zero

    asyncio.run(scenario())


def test_budgeted_model_client_rolls_back_parse_error_without_usage() -> None:
    """无法证明远端产生用量的解析异常仍应完整释放预留。"""

    async def scenario() -> None:
        error = ModelResponseParseError("safe parse failure")
        ledger = UsageLedger(BudgetLimits(max_model_calls=1))
        client = BudgetedModelClient(
            _ParseFailingModelClient(error),
            ledger,
            input_token_estimator=lambda request: 1,
        )
        request = ModelRequest(
            (ModelMessage(MessageRole.USER, "test"),),
            max_output_tokens=1,
            usage_scopes=("run",),
        )

        with pytest.raises(ModelResponseParseError) as captured:
            await client.generate(request)

        assert captured.value is error
        snapshot = ledger.snapshot("run")
        assert snapshot.model_calls == 0
        assert snapshot.active_model_calls == 0
        assert snapshot.reserved.is_zero

    asyncio.run(scenario())


def test_cost_limit_requires_explicit_rate_card() -> None:
    """配置费用上限时不能在缺少价格表的情况下按零费用放行。"""

    async def scenario() -> None:
        ledger = UsageLedger(BudgetLimits(max_cost_micros=10, cost_currency="USD"))
        client = BudgetedModelClient(_FailingModelClient(), ledger)
        request = ModelRequest(
            (ModelMessage(MessageRole.USER, "test"),),
            max_output_tokens=1,
            usage_scopes=("run",),
        )

        with pytest.raises(BudgetConfigurationError):
            await client.generate(request)

    asyncio.run(scenario())


def test_cost_limit_rejects_mismatched_rate_card_currency() -> None:
    """价格表币种与任一 scope 的费用上限不同时必须快速失败。"""

    async def scenario() -> None:
        ledger = UsageLedger(BudgetLimits(max_cost_micros=10, cost_currency="CNY"))
        card = TokenRateCard("USD", date(2026, 1, 1), 1, 1)
        client = BudgetedModelClient(_FailingModelClient(), ledger, rate_card=card)
        request = ModelRequest(
            (ModelMessage(MessageRole.USER, "test"),),
            max_output_tokens=1,
            usage_scopes=("run",),
        )

        with pytest.raises(BudgetConfigurationError, match="currency"):
            await client.generate(request)

    asyncio.run(scenario())


class _Continuation:
    @property
    def provider(self) -> str:
        return "test"


class _ContinuationModelClient:
    def __init__(self, ledger: UsageLedger) -> None:
        self._ledger = ledger
        self.continuation: ModelContinuation = _Continuation()
        self.reserved_input_on_second_call = 0
        self.calls = 0

    async def generate(self, request: ModelRequest) -> ModelResponse:
        self.calls += 1
        if self.calls == 1:
            return ModelResponse(
                usage=TokenUsage(input_tokens=3, output_tokens=2, total_tokens=5),
                continuation=self.continuation,
            )
        self.reserved_input_on_second_call = self._ledger.snapshot("run").reserved.input_tokens
        return ModelResponse(usage=TokenUsage(input_tokens=7, output_tokens=1, total_tokens=8))


def test_default_estimator_tracks_opaque_continuation_without_reading_it() -> None:
    """工具续轮应按上次实际上下文追加预留，且无需读取私有 continuation。"""

    async def scenario() -> None:
        ledger = UsageLedger(BudgetLimits(max_total_tokens=1000))
        delegate = _ContinuationModelClient(ledger)
        client = BudgetedModelClient(delegate, ledger, default_max_output_tokens=3)
        first = await client.generate(
            ModelRequest(
                (ModelMessage(MessageRole.USER, "first"),),
                usage_scopes=("run",),
            )
        )
        await client.generate(
            ModelRequest(
                (ModelMessage(MessageRole.USER, "next"),),
                continuation=first.continuation,
                usage_scopes=("run",),
            )
        )

        assert delegate.reserved_input_on_second_call >= first.usage.total_tokens
        assert ledger.snapshot("run").model_calls == 2

    asyncio.run(scenario())


class _Tool:
    spec = ToolSpec("safe", "安全测试工具", {"type": "object"})

    async def invoke(
        self,
        arguments: Mapping[str, object],
        context: ToolContext,
    ) -> ToolResult:
        del arguments, context
        return ToolResult("ok")


class _Executor:
    async def execute(self, step: PlanStep, context: LoopContext) -> ExecutionResult:
        del step, context
        return ExecutionResult("ok")


@dataclass(frozen=True)
class _AgentContext:
    team_run_id: str


class _Agent:
    spec = "agent-spec"

    async def execute(self, context: _AgentContext) -> str:
        del context
        return "ok"


def test_structural_wrappers_enforce_tool_executor_and_agent_dimensions() -> None:
    """三个非模型代理均应在第二次调用前拒绝对应维度超额。"""

    async def scenario() -> None:
        ledger = UsageLedger(BudgetLimits(max_tool_calls=1, max_attempts=1, max_agent_tasks=1))
        tool = BudgetedTool(_Tool(), ledger)
        executor = BudgetedExecutor(_Executor(), ledger)
        agent = BudgetedAgentEndpoint(_Agent(), ledger)
        tool_context = ToolContext("run")
        loop_context = LoopContext(LoopRequest("预算测试"), run_id="run")
        agent_context = _AgentContext("run")

        assert (await tool.invoke({}, tool_context)).content == "ok"
        assert (await executor.execute(PlanStep("执行"), loop_context)).output == "ok"
        assert await agent.execute(agent_context) == "ok"
        with pytest.raises(ResourceLimitExceededError):
            await tool.invoke({}, tool_context)
        with pytest.raises(ResourceLimitExceededError):
            await executor.execute(PlanStep("重试"), loop_context)
        with pytest.raises(ResourceLimitExceededError):
            await agent.execute(agent_context)

    asyncio.run(scenario())
