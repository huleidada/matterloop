# matterloop-policies

> 跨模块生产拓扑、资源所有权与上线检查见[企业集成指南](../docs/enterprise-integration.md)。

`matterloop-policies` 提供可组合的本地计算额度、用量账本、模型/工具/Executor/Agent 预算代理，以及 Loop 的重试、停止、审批和工具权限策略。

策略包不构造供应商客户端、不读取 `.env`、环境变量、密钥或价格，也不会把 Token、费用和工具调用次数写入 `LoopContext.metadata`。价格表、额度、作用域和底层组件都由应用组合根显式注入。

## 组件关系

```text
ModelRequest.usage_scopes
        │
        ▼
BudgetedModelClient ── reserve ──┐
BudgetedTool        ── reserve ──┤
BudgetedExecutor    ── reserve ──┼─> UsageLedger
BudgetedAgentEndpoint ─ reserve ─┘      │
                                         ├─ commit(actual)
                                         └─ rollback()

UsageLedger snapshot ──> BudgetPolicy ──> CompositeLoopPolicy ──> Core Loop
```

同一次调用可以同时归集到 team、child loop、task、agent 等多个 scope。账本先校验全部 scope，再在一个锁内写入全部预留，不会产生部分成功或并发超卖。

## 企业装配示例

```python
from datetime import date

from matterloop_policies import (
    BudgetLimits,
    BudgetedModelClient,
    TokenRateCard,
    UsageLedger,
)


def build_budgeted_model(
    user_created_model_client,
    *,
    input_micros_per_million: int,
    output_micros_per_million: int,
    pricing_effective_from: date,
):
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
        effective_from=pricing_effective_from,
        input_micros_per_million=input_micros_per_million,
        output_micros_per_million=output_micros_per_million,
    )
    return (
        BudgetedModelClient(
            user_created_model_client,
            ledger,
            rate_card=rate_card,
        ),
        ledger,
    )
```

若币种为 USD，`1 micro-USD = 0.000001 USD`。例如每百万 Token 价格为 0.28 USD 时，对应费率是 `280_000` micro-USD，而不是 `0.28`。

## 公共 API

| 分组 | 公共类型 |
| --- | --- |
| 额度配置 | `BudgetLimits`、`TokenRateCard` |
| 用量 DTO 与账本 | `UsageAmount`、`UsageSnapshot`、`UsageReservation`、`UsageLedger` |
| 预算代理 | `BudgetedModelClient`、`BudgetedTool`、`BudgetedExecutor`、`BudgetedAgentEndpoint`、`ScopeResolver` |
| Loop 策略 | `BudgetPolicy`、`CompositeLoopPolicy`、`NoProgressStopPolicy`、`ExponentialBackoffRetryPolicy` |
| 审批与权限 | `ApprovalRule`、`RuleBasedApprovalGate`、`AllowAllApproval`、`PermissionRule`、`RuleBasedPermissionPolicy` |
| 配置 | `RetryConfig`、`StopConfig` |
| 估算器 | `ModelInputTokenEstimator`、`estimate_utf8_input_tokens` |
| 异常 | `BudgetError`、`BudgetConfigurationError`、`ResourceLimitExceededError`、`UsageReservationError` |

## `BudgetLimits` 字段

所有资源上限默认 `None`，表示该维度不限制。显式上限必须至少为 1。

| 字段 | 默认值 | 资源语义 |
| --- | --- | --- |
| `max_model_calls` | `None` | 已结算与预留的模型调用数 |
| `max_concurrent_model_calls` | `None` | 当前活跃模型预留数 |
| `max_attempts` | `None` | Executor 尝试数 |
| `max_executor_attempts` | `None` | `max_attempts` 的兼容别名；两者同时设置时必须相等 |
| `max_input_tokens` | `None` | 输入 Token |
| `max_output_tokens` | `None` | 输出 Token |
| `max_total_tokens` | `None` | 总 Token；缓存与 reasoning 明细不重复累加 |
| `max_cache_hit_tokens` | `None` | 缓存命中输入明细 |
| `max_cache_miss_tokens` | `None` | 缓存未命中输入明细 |
| `max_reasoning_tokens` | `None` | reasoning 输出明细 |
| `max_cost_micros` | `None` | `cost_currency` 对应的 micro-unit 费用 |
| `cost_currency` | `"USD"` | 去空白并转大写；不得为空 |
| `max_tool_calls` | `None` | 工具调用数 |
| `max_agent_tasks` | `None` | Agent 任务数 |

额度是 scope 级硬上限，不是全局常量。可在 `UsageLedger(default_limits=...)` 设置默认值，再用 `configure_scope(scope, limits)` 覆盖特定 scope。

## 用量 DTO

### `UsageAmount`

| 字段 | 默认值 |
| --- | --- |
| `model_calls` | `0` |
| `input_tokens` | `0` |
| `output_tokens` | `0` |
| `total_tokens` | `0` |
| `cache_hit_tokens` | `0` |
| `cache_miss_tokens` | `0` |
| `reasoning_tokens` | `0` |
| `tool_calls` | `0` |
| `agent_tasks` | `0` |
| `attempts` | `0` |
| `costs_micros` | `{}`，按大写币种保存整数 micro-unit |

全部数值不得为负数。`cost_for(currency)` 返回指定币种费用；兼容属性 `cost_micros` 只返回 USD。`is_zero` 表示所有维度均为零。

`UsageSnapshot` 继承全部已结算字段，并增加：

| 字段 | 默认值 | 说明 |
| --- | --- | --- |
| `active_model_calls` | `0` | 当前未完成的模型调用预留数 |
| `reserved` | `UsageAmount()` | 尚未 commit/rollback 的预留总量 |

`UsageReservation` 包含 `reservation_id`、`scopes` 与 `amount`。它只是一张未结算凭证，不能跨账本使用，也不能重复提交或回滚。

## `UsageLedger` 协议与事务语义

| 方法 | 参数 | 行为 |
| --- | --- | --- |
| `__init__` | `default_limits=None` | 创建线程安全的进程内账本 |
| `configure_scope` | `scope, limits` | 设置 scope 上限并立即验证已有占用 |
| `limits_for` | `scope` | 返回显式上限或默认上限 |
| `has_cost_limit` | `scopes` | 判断任一 scope 是否启用费用上限 |
| `cost_limit_currencies` | `scopes` | 返回所有启用费用上限的币种集合 |
| `reserve` | `scopes, amount, reservation_id=None` | 在全部 scope 上原子预留；空申请、空/重复 scope 非法 |
| `commit` | `reservation, actual=None` | 释放预留并结算实际用量；`actual=None` 使用预留量 |
| `rollback` | `reservation` | 原子释放失败调用的预留，不增加已结算用量 |
| `consume` | `scopes, amount` | 对无需跨 `await` 的计数原子校验并直接结算 |
| `record_model_usage` | Token、缓存、reasoning、费用字段 | 兼容性便捷方法，记录一次已完成模型调用 |
| `record_tool_call` / `record_agent_task` / `record_attempt` | `scopes` | 直接累计对应资源 |
| `snapshot` | `scope` | 返回不可变已结算、预留和活跃快照 |
| `clear` | `scope` | 仅在没有活跃预留时清理 scope |

`reserve()` 中的 `amount.model_calls` 同时增加 `active_model_calls`，因此并发限制和总调用限制在同一临界区内检查。相同调用可传入多个 scope；任一 scope 超限时全部不写入。

通常实际用量小于保守预留。若供应商报告的实际用量大于预留，`commit()` 会先如实写入已发生消耗并释放预留，再抛 `ResourceLimitExceededError`，从而保留审计事实并阻止后续调用。

## 显式价格表

`TokenRateCard` 没有供应商默认价格，也不会联网查询价格。

| 字段 | 默认值 | 说明 |
| --- | --- | --- |
| `currency` | 必填 | 非空并转大写 |
| `effective_from` | 必填 | 价格生效自然日，仅作为审计数据，不自动选择历史价格 |
| `input_micros_per_million` | 必填 | 普通输入每百万 Token 的 micro-unit 费率 |
| `output_micros_per_million` | 必填 | 普通输出费率 |
| `cache_hit_input_micros_per_million` | `None` | 未设置时回退普通输入费率 |
| `cache_miss_input_micros_per_million` | `None` | 未设置时回退普通输入费率 |
| `reasoning_output_micros_per_million` | `None` | 未设置时回退普通输出费率 |

所有费率必须为非负整数。`calculate_cost()` 按实际 Token 明细计算，并向上取整到一个 micro-unit；reasoning 最多按 `output_tokens` 计费。`estimate_max_cost()` 对输入侧和输出侧分别选择最高配置费率，形成调用前保守预留。

调用方应在部署配置中同时保存供应商、模型、区域、币种、生效日和价格来源，并在供应商调价后显式发布新配置。本包不会验证价格是否仍然有效。

## `BudgetedModelClient`

| 构造参数 | 默认值 | 说明 |
| --- | --- | --- |
| `client` | 必填 | 已构造的 `ModelClient` |
| `ledger` | 必填 | 共用额度账本 |
| `rate_card` | `None` | 任一目标 scope 设置费用上限时必填 |
| `default_scopes` | `("global",)` | 请求未提供 `usage_scopes` 时使用 |
| `default_max_output_tokens` | `4096` | 请求未设置输出上限时的预留值，至少为 1 |
| `input_token_estimator` | `None` | 自定义保守估算器；默认使用 UTF-8 字节上界 |

一次 `generate()` 的流程为：

1. 使用 `request.usage_scopes`，为空时使用 `default_scopes`；
2. 校验费用上限是否有同币种 rate card；
3. 估算输入，按请求或默认输出上限预留调用数、并发、Token、缓存、reasoning 和费用；
4. 在预留持有期间调用底层模型；
5. 成功时按 `ModelResponse.usage` 结算；
6. `ModelResponseParseError` 携带 usage 时按实际用量结算，否则回滚；
7. 其他异常和取消回滚预留。

默认 `estimate_utf8_input_tokens()` 统计消息、工具定义、工具输出和 response schema 的可见 UTF-8 字节，不保存文本，也不检查 metadata、凭据或 continuation。它是无 tokenizer 时的保守近似，不是供应商精确 Token 计数。

opaque continuation 会隐藏历史。默认估算器只接受由同一个 `BudgetedModelClient` 产生并跟踪的 continuation，并把上一事务总 Token 加入估算；最多跟踪 1024 个 continuation。跨进程恢复、跨包装器复用或自定义 continuation 必须注入能够覆盖完整历史的自定义估算器，否则抛 `BudgetConfigurationError`。

## 工具、Executor 与 Agent 预算代理

| 代理 | 计量资源 | 默认 scope resolver |
| --- | --- | --- |
| `BudgetedTool` | `tool_calls=1` | `(ToolContext.run_id,)` |
| `BudgetedExecutor` | `attempts=1` | `(LoopContext.run_id,)` |
| `BudgetedAgentEndpoint` | `agent_tasks=1` | `(context.team_run_id,)` |

三个代理都在进入底层组件前 reserve，正常返回后 commit，异常或取消时 rollback，并原样暴露底层 `spec`。自定义上下文或多层汇总应通过 `scope_resolver(context) -> str | Iterable[str]` 返回显式 scopes，例如：

```python
def agent_scopes(context):
    return (
        f"team:{context.team_run_id}",
        f"task:{context.team_run_id}:{context.task.task_id}",
        f"agent:{context.agent_id}",
    )
```

这些包装器的已结算计数表示底层调用正常返回。底层在抛出异常前已产生的外部副作用无法由账本回滚；若失败调用也必须计数，应在业务边界用 `consume()` 或专用包装器记录。

## Loop 策略配置

### 预算与组合

`BudgetPolicy(limits, ledger)` 使用 `context.run_id` 查询快照，并同时考虑已结算与预留资源。达到上限时返回 `False`，阻止新一轮继续。真正的并发硬边界仍由 `UsageLedger.reserve()` 保证。

`CompositeLoopPolicy(*policies)` 按注入顺序短路执行，只有全部 `can_continue(context)` 返回真时才继续。

### 重试

| `RetryConfig` 字段 | 默认值 |
| --- | --- |
| `max_attempts` | `3` |
| `base_delay_seconds` | `0.5` |
| `max_delay_seconds` | `30` |
| `jitter_ratio` | `0.2`，范围 0–1 |

`ExponentialBackoffRetryPolicy(config=None, retryable=(TimeoutError, ConnectionError), random_source=None)` 只重试显式异常类型，达到最大尝试数或遇到其他异常时返回 FAIL。延迟为有上限的指数退避并加入正负抖动。生产测试可注入固定 `random.Random`。

不要把 `ResourceLimitExceededError` 加入 retryable；额度耗尽不会因等待而恢复。

### 无进展停止

`StopConfig(max_identical_feedback=2)` 至少为 1。`NoProgressStopPolicy` 检查最近失败验证反馈；连续达到阈值且文本完全相同时返回 `False`。

### 审批

| 类型 | 字段/参数 | 默认行为 |
| --- | --- | --- |
| `ApprovalRule` | `executor`、`decision` 必填 | `executor="*"` 可匹配全部 |
| `RuleBasedApprovalGate` | `rules=()`、`default=ApprovalDecision.DEFERRED` | 按顺序采用第一条命中规则 |
| `AllowAllApproval` | 无 | 始终 APPROVED，仅适用于明确无需人工审批的装配 |

### 工具权限

| 类型 | 字段/参数 | 默认行为 |
| --- | --- | --- |
| `PermissionRule` | `tool`、`operations`、`decision` 必填 | `tool="*"` 或 operations 含 `"*"` 可通配 |
| `RuleBasedPermissionPolicy` | `rules=()`、`default=PermissionDecision.DENY` | 按顺序采用第一条命中规则 |

权限策略从工具参数的 `operation` 字段读取操作名；缺失或非字符串时使用 `"invoke"`。规则只做字符串匹配，不解析 Shell argv、文件路径、HTTP URL 或业务对象；需要更细边界时应实现自定义 `ToolAuthorizer`。

## 异常与上层映射

| 异常 | 处理建议 |
| --- | --- |
| `ResourceLimitExceededError` | 不重试；Core/Team 应映射为 `BUDGET_EXHAUSTED` |
| `BudgetConfigurationError` | 部署配置错误，例如费用上限缺少 rate card、币种不一致或 continuation 无法估算 |
| `UsageReservationError` | 预留未知、重复结算、重复回滚，或清理仍有活跃预留的 scope |
| `BudgetError` | 本模块异常基类 |

`ResourceLimitExceededError` 只保存 `scope`、`resource`、`limit`、`current`、`requested`，不会拼接模型请求、供应商响应或凭据。资源名包括 `model_calls`、`concurrent_model_calls`、各 Token 维度、`tool_calls`、`agent_tasks`、`attempts` 和 `cost_micros:<CURRENCY>`。

## 并发、生命周期与持久化边界

- `UsageLedger` 使用进程内可重入锁，线程安全但不是跨进程分布式账本。
- 多个 Worker 或服务实例共享硬额度时，必须实现外部原子账本；当前发行包没有 Redis/SQL 账本协议或实现。
- reservation 是内存对象，进程崩溃后不会自动恢复；不要将本实现视为持久化财务账本。
- `clear()` 只清理 scope 状态，不关闭模型、工具或 Agent 资源。
- 包装器不接管底层组件生命周期；关闭顺序和所有权由组合根管理。
- rate card 计算的是估算费用，不查询供应商账单，也不处理税费、阶梯价、批处理折扣、免费额度或汇率。

## 敏感信息边界

- scope 会出现在超限异常中，应使用内部稳定标识，不要直接放置用户邮箱、密钥或完整业务文本。
- 账本只保存整数计数和币种费用，不保存提示词、模型输出、工具结果或 reasoning 内容。
- 自定义 `scope_resolver`、估算器和策略可能接触完整上下文；其日志与异常由调用方负责脱敏。
- `ApprovalRule` 与 `PermissionRule` 是静态字符串规则，不是身份认证、租户隔离或审计系统。
- 本地额度是应用侧保护，不代表供应商账户余额、速率限制或最终账单。

## 当前限制

- 只提供进程内账本，不提供持久化、跨进程 CAS、额度补充或时间窗口重置。
- 每个 scope 只能配置一个费用币种上限；`UsageAmount` 可记录多币种，但不会换汇或合并。
- 不内置任何供应商价格，也不自动选择价格生效版本。
- 默认 Token 估算器不是 tokenizer；需要严格费用边界时应注入供应商兼容估算器并保留足够安全余量。
- 重试、审批、权限和停止策略是可替换基础实现，不包含组织身份、RBAC、ABAC 或人工任务系统集成。
