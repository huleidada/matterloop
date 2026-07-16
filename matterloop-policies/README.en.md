[简体中文](https://github.com/huleidada/matterloop/blob/main/matterloop-policies/README.md) | English

# matterloop-policies

The policy package answers two questions: how many more resources a run may consume, and whether
the next action is allowed to proceed. Budgets, retries, approvals, and tool permissions are
independently replaceable. None of them is hidden inside `LoopContext.metadata`.

```bash
pip install matterloop-policies
```

## Why budgets need reservations

If parallel Agents record usage only after a call completes, they can collectively exceed the
limit. `UsageLedger` reserves a conservative amount before an external call starts, settles the
actual usage on success, and rolls the reservation back on failure:

```text
BudgetedModelClient ─┐
BudgetedTool        ─┼─ reserve(scopes, estimate)
BudgetedExecutor    ─┤        ├─ commit(actual)
BudgetedAgentEndpoint┘        └─ rollback()
```

A single call may be charged to team, run, task, and agent scopes at the same time. If any scope
would exceed its limit, no part of the reservation is written. This prevents a partial commit in
which the parent ledger succeeds while a child ledger fails.

## Configuring model budgets

```python
from datetime import date

from matterloop_policies import (
    BudgetLimits,
    BudgetedModelClient,
    TokenRateCard,
    UsageLedger,
)

limits = BudgetLimits(
    max_model_calls=12,
    max_concurrent_model_calls=2,
    max_total_tokens=40_000,
    max_cost_micros=30_000,
    cost_currency="USD",
)
ledger = UsageLedger(default_limits=limits)
rate_card = TokenRateCard(
    currency="USD",
    effective_from=date(2026, 7, 16),
    input_micros_per_million=input_rate,
    output_micros_per_million=output_rate,
)
model = BudgetedModelClient(model_client, ledger, rate_card=rate_card)
```

The application must provide pricing. This package does not download provider prices and has no
knowledge of account discounts, free tiers, taxes, or the final invoice. A micro-USD is one
millionth of a US dollar; 0.28 USD is `280_000` micro-USD.

## Resources that can be limited

Every limit in `BudgetLimits` defaults to `None`, meaning unlimited:

`max_model_calls`, `max_concurrent_model_calls`, `max_attempts`, `max_executor_attempts`,
`max_input_tokens`, `max_output_tokens`, `max_total_tokens`, `max_cache_hit_tokens`,
`max_cache_miss_tokens`, `max_reasoning_tokens`, `max_cost_micros`, `cost_currency`,
`max_tool_calls`, and `max_agent_tasks`.

`max_executor_attempts` is a compatibility alias for `max_attempts`; if both are set, their values
must be equal. Cache and reasoning details are not added to the Token total a second time. Cost is
limited independently per `cost_currency`; the ledger does not convert currencies.

## Ledger transactions

The core operations are `reserve(scopes, amount)`, `commit(reservation, actual)`, and
`rollback(reservation)`. Use `consume()` for direct counters that do not span an `await`.
`snapshot(scope)` returns settled usage, reserved usage, and active model calls together.

- `UsageAmount` represents one increment with `model_calls`, `input_tokens`, `output_tokens`,
  `total_tokens`, `cache_hit_tokens`, `cache_miss_tokens`, `reasoning_tokens`, `tool_calls`,
  `agent_tasks`, `attempts`, and currency-keyed `costs_micros`.
- `UsageReservation(reservation_id, scopes, amount)` is a one-time ledger credential. It cannot be
  committed by another ledger or settled more than once.
- `UsageSnapshot` retains every counter from `UsageAmount` and adds `active_model_calls` and
  `reserved`, providing an immutable view of one scope.

If actual usage is greater than the reservation, `commit()` first records the usage that has
already occurred and then raises `ResourceLimitExceededError`. This prevents auditing from
"rolling back" a paid call and blocks subsequent calls correctly.

`UsageLedger` is thread-safe only within the current process. Enforcing a hard shared budget across
multiple service instances requires an external ledger with atomic reservations. This package does
not currently provide a Redis or SQL implementation, and reservations do not survive a process
crash.

## Rate cards

`TokenRateCard` uses integer micro-units. Its fields are `currency`, `effective_from`,
`input_micros_per_million`, `output_micros_per_million`,
`cache_hit_input_micros_per_million`, `cache_miss_input_micros_per_million`, and
`reasoning_output_micros_per_million`. An omitted cache rate falls back to the standard input rate;
an omitted reasoning rate falls back to the standard output rate. `effective_from` is audit
metadata only. It does not select historical rate cards automatically.

## Where to apply budget wrappers

- `BudgetedModelClient` reserves model calls, concurrency, Tokens, and cost. It prefers scopes from
  `ModelRequest.usage_scopes`.
- `BudgetedTool` increments `tool_calls` once per call.
- `BudgetedExecutor` increments `attempts` each time execution starts.
- `BudgetedAgentEndpoint` increments `agent_tasks` once per team task.

The last three wrappers accept a `scope_resolver(context)` that may return parent and child scopes
together. Wrappers do not own the lifecycle of the wrapped component. If a failed call has already
caused external side effects, rolling back its ledger reservation cannot undo those effects.

The default model Token estimator uses visible UTF-8 bytes as a conservative approximation; it is
not a provider tokenizer. An opaque continuation can be estimated only while it remains tracked by
the same `BudgetedModelClient`. Cross-process or custom continuations require a caller-supplied
estimator.

## Loop decision policies

### Retry and stop

`RetryConfig(max_attempts, base_delay_seconds, max_delay_seconds, jitter_ratio)` defaults to `3`,
`0.5` seconds, `30` seconds, and `0.2`, respectively. `ExponentialBackoffRetryPolicy` retries only
the configured exception types. Do not classify budget exhaustion as retryable.

`StopConfig(max_identical_feedback)` defaults to `2`. `NoProgressStopPolicy` stops after the same
failure feedback repeats consecutively, preventing the Loop from consuming the remaining budget
with an unchanged action. `CompositeLoopPolicy` combines multiple continuation conditions and
short-circuits in order.

### Approval and permissions

`ApprovalRule(executor, decision)` matches an executor; `executor="*"` is a wildcard.
`RuleBasedApprovalGate` returns `DEFERRED` by default. `AllowAllApproval` is appropriate only for an
assembly that explicitly requires no human approval.

`PermissionRule(tool, operations, decision)` matches the tool name and operation string.
`RuleBasedPermissionPolicy` denies by default. It does not interpret Shell argv, file paths, or
URLs. Complex policies should implement a custom `ToolAuthorizer` and include identity and tenant
context.

## Errors and security boundaries

`ResourceLimitExceededError` is a hard-stop signal. Core and Team should map it to
`BUDGET_EXHAUSTED` without retrying. `BudgetConfigurationError` indicates invalid pricing,
currency, or continuation-estimation configuration. `UsageReservationError` indicates an unknown
reservation or an attempt to process one more than once.

The ledger stores integer counters only; it does not store prompts, model output, or reasoning.
Scopes appear in exceptions and diagnostics, so use internal identifiers rather than email
addresses, credentials, or business content. Local budgets do not represent a provider's live
balance or account-level rate limits. See the
[Enterprise Integration Guide](../docs/enterprise-integration.en.md) for a complete assembly.
