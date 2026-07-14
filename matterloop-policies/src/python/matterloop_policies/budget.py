"""Loop 资源预算策略。"""

from dataclasses import dataclass

from matterloop_core import LoopContext, LoopPolicy

from matterloop_policies.usage import UsageLedger


@dataclass(frozen=True, slots=True)
class BudgetLimits:
    """定义一个 usage scope 的可选资源硬上限。

    费用使用 micro-unit 整数，避免浮点误差。``cost_currency`` 决定
    ``max_cost_micros`` 对应的币种；库不会内置任何供应商价格。
    """

    max_model_calls: int | None = None
    max_concurrent_model_calls: int | None = None
    max_attempts: int | None = None
    max_executor_attempts: int | None = None
    max_input_tokens: int | None = None
    max_output_tokens: int | None = None
    max_total_tokens: int | None = None
    max_cache_hit_tokens: int | None = None
    max_cache_miss_tokens: int | None = None
    max_reasoning_tokens: int | None = None
    max_cost_micros: int | None = None
    cost_currency: str = "USD"
    max_tool_calls: int | None = None
    max_agent_tasks: int | None = None

    def __post_init__(self) -> None:
        """禁止非正数预算造成不可理解的运行行为。"""
        if (
            self.max_attempts is not None
            and self.max_executor_attempts is not None
            and self.max_attempts != self.max_executor_attempts
        ):
            raise ValueError("attempt limit aliases must match")
        attempt_limit = (
            self.max_attempts if self.max_attempts is not None else self.max_executor_attempts
        )
        object.__setattr__(self, "max_attempts", attempt_limit)
        object.__setattr__(self, "max_executor_attempts", attempt_limit)
        for value in (
            self.max_model_calls,
            self.max_concurrent_model_calls,
            attempt_limit,
            self.max_input_tokens,
            self.max_output_tokens,
            self.max_total_tokens,
            self.max_cache_hit_tokens,
            self.max_cache_miss_tokens,
            self.max_reasoning_tokens,
            self.max_cost_micros,
            self.max_tool_calls,
            self.max_agent_tasks,
        ):
            if value is not None and value < 1:
                raise ValueError("budget limits must be positive")
        currency = self.cost_currency.strip().upper()
        if not currency:
            raise ValueError("cost currency must not be empty")
        object.__setattr__(self, "cost_currency", currency)


class BudgetPolicy:
    """根据独立用量账本决定 Loop 是否可以继续。"""

    def __init__(self, limits: BudgetLimits, ledger: UsageLedger) -> None:
        self._limits = limits
        self._ledger = ledger

    def can_continue(self, context: LoopContext) -> bool:
        """判断当前用量是否仍处于全部预算以内。"""
        usage = self._ledger.snapshot(context.run_id)
        reserved = usage.reserved
        checks = (
            (usage.model_calls + reserved.model_calls, self._limits.max_model_calls),
            (
                usage.active_model_calls,
                self._limits.max_concurrent_model_calls,
            ),
            (usage.attempts + reserved.attempts, self._limits.max_attempts),
            (usage.input_tokens + reserved.input_tokens, self._limits.max_input_tokens),
            (
                usage.output_tokens + reserved.output_tokens,
                self._limits.max_output_tokens,
            ),
            (usage.total_tokens + reserved.total_tokens, self._limits.max_total_tokens),
            (
                usage.cache_hit_tokens + reserved.cache_hit_tokens,
                self._limits.max_cache_hit_tokens,
            ),
            (
                usage.cache_miss_tokens + reserved.cache_miss_tokens,
                self._limits.max_cache_miss_tokens,
            ),
            (
                usage.reasoning_tokens + reserved.reasoning_tokens,
                self._limits.max_reasoning_tokens,
            ),
            (
                usage.cost_for(self._limits.cost_currency)
                + reserved.cost_for(self._limits.cost_currency),
                self._limits.max_cost_micros,
            ),
            (usage.tool_calls + reserved.tool_calls, self._limits.max_tool_calls),
            (usage.agent_tasks + reserved.agent_tasks, self._limits.max_agent_tasks),
        )
        return all(limit is None or value < limit for value, limit in checks)


class CompositeLoopPolicy:
    """要求全部子策略同时允许继续。"""

    def __init__(self, *policies: LoopPolicy) -> None:
        self._policies = policies

    def can_continue(self, context: LoopContext) -> bool:
        """依次执行子策略并进行短路判断。"""
        return all(policy.can_continue(context) for policy in self._policies)
