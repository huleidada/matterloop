简体中文 | [English](https://github.com/huleidada/matterloop/blob/main/matterloop-policies/README.en.md)

# matterloop-policies

策略模块回答两类问题：一次运行还可以消耗多少资源，以及下一步是否允许继续。额度、重试、审批
和工具权限都可以独立替换；它们不会被藏进 `LoopContext.metadata`。

```bash
pip install matterloop-policies
```

## 额度为什么需要 reserve

并行 Agent 如果在调用完成后才记账，会一起穿透上限。`UsageLedger` 在进入外部调用前预留保守
用量，成功后按实际 usage 结算，失败则回滚：

```text
BudgetedModelClient ─┐
BudgetedTool        ─┼─ reserve(scopes, estimate)
BudgetedExecutor    ─┤        ├─ commit(actual)
BudgetedAgentEndpoint┘        └─ rollback()
```

同一调用可以同时计入 team、run、task 和 agent scope。任一 scope 超限，整笔预留都不会写入，
因此不会出现父账本成功、子账本失败的半提交。

## 模型额度装配

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

价格必须由应用提供。本包不下载供应商价格，也不知道账号折扣、免费额度、税费或最终账单。
micro-USD 是百万分之一美元；0.28 USD 对应 `280_000` micro-USD。

## 可限制的资源

`BudgetLimits` 的所有上限默认 `None`（不限制）：

`max_model_calls`、`max_concurrent_model_calls`、`max_attempts`、`max_executor_attempts`、
`max_input_tokens`、`max_output_tokens`、`max_total_tokens`、`max_cache_hit_tokens`、
`max_cache_miss_tokens`、`max_reasoning_tokens`、`max_cost_micros`、`cost_currency`、
`max_tool_calls`、`max_agent_tasks`。

`max_executor_attempts` 是 `max_attempts` 的兼容别名；同时设置时必须相等。Token 总量不重复累加
缓存和 reasoning 明细。费用按 `cost_currency` 单独约束，不做汇率换算。

## 账本事务

核心操作是 `reserve(scopes, amount)`、`commit(reservation, actual)` 和 `rollback(reservation)`。
`consume()` 用于不跨 await 的直接计数。`snapshot(scope)` 同时返回已结算、预留和活跃模型调用。

- `UsageAmount` 用 `model_calls`、`input_tokens`、`output_tokens`、`total_tokens`、
  `cache_hit_tokens`、`cache_miss_tokens`、`reasoning_tokens`、`tool_calls`、`agent_tasks`、`attempts`
  和按币种保存的 `costs_micros` 表示一次增量。
- `UsageReservation(reservation_id, scopes, amount)` 是账本内的一次性凭证，不能跨账本或重复结算。
- `UsageSnapshot` 保留 `UsageAmount` 的全部计数字段，并增加 `active_model_calls` 和 `reserved`，表示
  某个 scope 的不可变视图。

实际 usage 大于预留时，`commit()` 会先记录已经发生的消耗，再抛
`ResourceLimitExceededError`。这样审计不会把已付费调用“回滚掉”，后续调用也会被阻止。

`UsageLedger` 只在当前进程内线程安全。多个服务实例共享硬额度时，需要一个具备原子预留的外部
账本；当前包没有 Redis/SQL 实现，reservation 也不会在进程崩溃后恢复。

## 价格表

`TokenRateCard` 使用整数 micro-unit，字段为 `currency`、`effective_from`、
`input_micros_per_million`、`output_micros_per_million`、`cache_hit_input_micros_per_million`、
`cache_miss_input_micros_per_million` 和 `reasoning_output_micros_per_million`。
缓存费率未设置时回退普通输入费率，reasoning 费率未设置时回退普通输出费率。`effective_from`
只用于审计，不会自动选择历史版本。

## 包装位置

- `BudgetedModelClient` 预留模型调用、并发、Token 和费用；scope 优先取
  `ModelRequest.usage_scopes`。
- `BudgetedTool` 每次调用计一个 `tool_calls`。
- `BudgetedExecutor` 每次进入执行器计一个 `attempts`。
- `BudgetedAgentEndpoint` 每个团队任务计一个 `agent_tasks`。

后三者可用 `scope_resolver(context)` 同时返回父子 scope。包装器不接管底层组件生命周期；失败
调用若已经产生外部副作用，账本回滚也无法撤销副作用。

默认模型 Token 估算器以可见 UTF-8 字节形成保守近似，不是供应商 tokenizer。opaque continuation
只有在同一个 `BudgetedModelClient` 内跟踪时才能估算；跨进程或自定义 continuation 应注入自己的
估算器。

## Loop 决策策略

### 重试与停止

`RetryConfig(max_attempts, base_delay_seconds, max_delay_seconds, jitter_ratio)` 默认值为 3、0.5 秒、
30 秒和 0.2。`ExponentialBackoffRetryPolicy` 只重试配置的异常类型；不要把额度耗尽加入
retryable。

`StopConfig(max_identical_feedback)` 默认 2。`NoProgressStopPolicy` 在连续失败反馈完全相同时停止，
避免 Loop 用相同动作消耗剩余预算。`CompositeLoopPolicy` 按顺序短路组合多个继续条件。

### 审批与权限

`ApprovalRule(executor, decision)` 按执行器匹配，`executor="*"` 是通配；
`RuleBasedApprovalGate` 默认返回 `DEFERRED`。`AllowAllApproval` 只适合明确无需人工审批的装配。

`PermissionRule(tool, operations, decision)` 按工具名和 operation 字符串匹配；
`RuleBasedPermissionPolicy` 默认拒绝。它不会理解 Shell argv、文件路径或 URL，复杂策略应实现
自定义 `ToolAuthorizer` 并结合身份与租户信息。

## 错误与安全边界

`ResourceLimitExceededError` 是硬停止信号，Core/Team 应映射为 `BUDGET_EXHAUSTED`，不重试。
`BudgetConfigurationError` 表示价格、币种或 continuation 估算配置错误；
`UsageReservationError` 表示未知或重复处理 reservation。

账本只保存整数计数，不保存提示词、模型输出或 reasoning。scope 会出现在异常与诊断中，应使用
内部 ID，不要放邮箱、密钥或业务正文。本地额度也不代表供应商实时余额或账号限流。企业组合示例
见[企业集成指南](../docs/enterprise-integration.md)。
