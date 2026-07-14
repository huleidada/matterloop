# matterloop-core

> 跨模块生产拓扑、资源所有权与上线检查见[企业集成指南](../docs/enterprise-integration.md)。

`matterloop-core` 是 MatterLoop 的零第三方运行时依赖闭环内核。源码包直接位于
`src/python/matterloop_core`，统一通过 `from matterloop_core import ...` 使用；不提供旧的
`core` 兼容包。

内核只负责流程编排、状态、上下文、生命周期事件和扩展协议。模型、Agent、工具、策略、
内存检查点、队列和 Web 集成由独立发行包提供。

## 安装与检查

```bash
pip install matterloop-core
```

仓库内开发：

```bash
cd matterloop-core
python -m pip install -e ".[dev]"
python -m pytest
python -m ruff check .
python -m mypy src/python/matterloop_core tests
```

## 源码职责

```text
src/python/matterloop_core/
├── loop/           # Planner → Executor → Verifier 闭环控制与恢复
├── context/        # 请求、预算、计划、证据、上下文和检查点编解码
├── state/          # 生命周期状态、停止原因和恢复模式
├── events/         # 稳定事件模型与进程内发布器
├── protocols/      # 所有可选模块实现的结构化协议
├── registry/       # 组件规格、插件工厂目录和热插拔注册中心
├── control/        # 审批与重试的标准决策值对象
└── exceptions/     # 稳定的核心异常类型
```

每个子包的 `__init__.py` 只导出公共 API，不承载业务实现。`loop` 可以使用其他核心子包，
其他子包不反向依赖 `loop`。核心不提供默认策略和具体检查点存储，防止内核与运行环境耦合。

## 组合 Loop

调用方需要显式注入协议实现；组件可以来自 MatterLoop 其他发行包，也可以是业务自定义实现：

```python
from matterloop_core import (
    AgentLoop,
    ComponentRegistry,
    Executor,
    LoopLimits,
    LoopRequest,
    Planner,
    Verifier,
)

planners = ComponentRegistry[Planner]()
executors = ComponentRegistry[Executor]()
verifiers = ComponentRegistry[Verifier]()

planners.register("default", my_planner)
executors.register("default", my_executor)
verifiers.register("default", my_verifier)

loop = AgentLoop(
    planners=planners,
    executors=executors,
    verifiers=verifiers,
    checkpoint_store=my_checkpoint_store,
    policy=my_loop_policy,
    events=my_event_publisher,
    approval_gate=my_approval_gate,
    retry_policy=my_retry_policy,
)

result = await loop.run(
    LoopRequest(
        goal="完成工程任务",
        acceptance_criteria=("测试通过", "结果可以交付"),
        limits=LoopLimits(
            max_cycles=5,
            max_attempts=20,
            max_steps_per_plan=10,
            timeout_seconds=300,
        ),
    )
)
```

协议使用 `typing.Protocol`，扩展实现无需继承 MatterLoop 基类。

## 闭环与预算语义

- `max_cycles` 限制规划轮次；验证失败或执行器请求重新规划会消耗新轮次。
- `max_attempts` 限制执行器调用总数；异常重试会继续消耗该预算，但不增加规划轮次。
- `max_steps_per_plan` 限制单次规划返回的步骤数量。
- `timeout_seconds` 只累计活跃执行时间；人工等待不计时，暂停恢复也不会重置已用时间。
- `PlanStep.executor` 为每个步骤选择执行器，控制器在调用前重新从注册中心解析实例。
- 只有 `PlanStep.requires_approval=True` 的步骤会调用审批门。

## 暂停与恢复

```python
from matterloop_core import HumanAction, HumanResponse, ResumeMode

# 审批门暂缓或整体验收请求人工处理时，结果携带结构化请求。
interaction = paused.pending_interaction
assert interaction is not None

# 提交反馈只改变检查点，不会在请求线程内隐式恢复执行。
await loop.submit_human_response(
    paused.run_id,
    HumanResponse(interaction.interaction_id, HumanAction.APPROVE),
)

# 默认精确继续检查点中的当前步骤，不会再次规划。
continued = await loop.resume(run_id)

# 显式丢弃旧计划并开始新一轮规划。
replanned = await loop.resume(run_id, mode=ResumeMode.REPLAN)
```

`ResumeMode.CONTINUE` 缺少未完成计划时会抛出 `LoopNotResumableError`，不会静默切换为重新
规划。`REVISE` 和 `PROVIDE_INPUT` 会保留完整反馈历史，并让下一次恢复强制重新规划；相同
幂等键和相同内容的重复反馈是 no-op。取消是协作式的，`loop.cancel(run_id)` 会在下一个
安全边界生效。

可选 `CompletionEvaluator` 会在全部步骤通过后验收整体目标，并返回 `ACCEPT`、`REPLAN`、
`REQUEST_HUMAN` 或 `STOP`，避免只凭局部步骤成功就结束运行。

## 检查点 JSON

`LoopCheckpointCodec` 提供 schema v2 的严格 JSON 往返能力，包含人工交互、事件序号、
活跃计时和 CAS revision：

```python
from matterloop_core import LoopCheckpointCodec

codec = LoopCheckpointCodec()
payload = codec.dumps(context)
restored = codec.loads(payload)
```

未知版本、字段类型错误、无时区时间以及不可 JSON 序列化的元数据都会抛出
`CheckpointSchemaError`。

`CheckpointStore.save(context, expected_revision=...)` 必须原子比较 revision，成功后返回递增
revision；并发提交冲突抛出 `CheckpointConflictError`，不得覆盖较新的检查点。

## 工厂插件与热插拔

```python
from matterloop_core import (
    ComponentRegistry,
    ComponentSpec,
    FactoryCatalog,
    PluginDefinition,
)

plugin = PluginDefinition(
    name="company-agents",
    version="1.0.0",
    components=(
        ComponentSpec("company", create_company_executor, capabilities=frozenset({"execute"})),
    ),
)

catalog = FactoryCatalog[object]()
catalog.install(plugin)

registry = ComponentRegistry[object]()
registry.install(catalog)
registry.register("company", replacement, replace=True)
```

注册中心的替换只影响后续查询，已开始调用的方法仍持有原实例。批量工厂安装会先创建所有
实例，再进行一次原子更新；任一工厂失败时不会留下部分注册结果。

第三方包可以在独立 Entry Point 分组中导出 `PluginDefinition` 或返回该定义的无参函数。
`FactoryCatalog.discover()` 和 `ComponentRegistry.discover()` 都必须由调用方显式触发，普通
导入不会执行第三方插件代码。

## 稳定公共入口

包级 `matterloop_core.__all__` 是跨模块使用的稳定入口：

| 分组 | 公共 API |
|---|---|
| 编排 | `AgentLoop`、`result_from_context` |
| 请求、计划与结果 | `LoopLimits`、`LoopRequest`、`Plan`、`PlanStep`、`ArtifactRef`、`ExecutionResult`、`VerificationResult`、`IterationRecord`、`LoopContext`、`LoopResult` |
| 人工交互 | `HumanInteractionKind`、`HumanAction`、`HumanInteractionRequest`、`HumanResponse`、`HumanInteractionRecord` |
| 决策与状态 | `ApprovalDecision`、`RetryAction`、`RetryDecision`、`CompletionAction`、`CompletionDecision`、`LoopStatus`、`StopReason`、`ResumeMode`、`ensure_transition` |
| 扩展协议 | `Planner`、`Executor`、`Verifier`、`CheckpointStore`、`LoopPolicy`、`EventPublisher`、`ApprovalGate`、`RetryPolicy`、`CompletionEvaluator` |
| 事件 | `LoopEventType`、`LoopEvent`、`EventHandler`、`LocalEventPublisher` |
| 插件 | `ComponentSpec`、`PluginDefinition`、`FactoryCatalog`、`ComponentRegistry` |
| 异常 | `MatterLoopError` 及全部类型化子类，见“错误语义” |

## `AgentLoop` 构造与方法

### 构造参数

| 参数 | 类型 | 必填 | 默认 | 业务含义 | 生命周期与并发 |
|---|---|---:|---|---|---|
| `planners` | `ComponentRegistry[Planner]` | 是 | 无 | 按名称解析规划器 | `run/resume` 默认读取 `default`；每轮规划前重新解析 |
| `executors` | `ComponentRegistry[Executor]` | 是 | 无 | 按步骤解析执行器 | 每次执行按 `PlanStep.executor` 重新解析，热替换影响新调用 |
| `verifiers` | `ComponentRegistry[Verifier]` | 是 | 无 | 按名称解析验证器 | `run/resume` 默认读取 `default` |
| `checkpoint_store` | `CheckpointStore` | 是 | 无 | 保存所有状态变化和恢复游标 | 必须提供原子 revision CAS |
| `policy` | `LoopPolicy` | 是 | 无 | 每个安全边界判断是否继续 | 返回假时以 `POLICY_REJECTED` 阻塞 |
| `events` | `EventPublisher` | 是 | 无 | 发布生命周期审计事件 | 检查点先提交、事件后发布；发布失败不回滚检查点 |
| `approval_gate` | `ApprovalGate` | 是 | 无 | 审批显式标记的步骤 | 未标记步骤不会调用该组件 |
| `retry_policy` | `RetryPolicy` | 是 | 无 | 处理 Executor 抛出的普通异常 | 不处理 Planner、Verifier、审批、检查点或事件异常 |
| `completion_evaluator` | `CompletionEvaluator \| None` | 否 | `None` | 全部步骤通过后的整体目标验收 | 为空时直接完成；不由 Loop 管理关闭 |

`AgentLoop` 不取得上述组件的资源所有权，也没有 `aclose()`。连接池、模型客户端和工具生命周期
应交给 `matterloop-runtime` 或应用组合根管理。

### 公共方法

| 方法 | 关键参数与默认 | 返回值 | 语义与失败 |
|---|---|---|---|
| `create_run_id()` | 无 | `str` | 生成 UUID hex，便于在并发启动前获得 run id |
| `run(request, planner="default", verifier="default", run_id=None)` | request 必填 | `LoopResult` | 创建检查点并驱动至完成、暂停、阻塞、取消或超时 |
| `resume(run_id, mode=CONTINUE, planner="default", verifier="default")` | run_id 必填 | `LoopResult` | 只允许从 `PAUSED/BLOCKED` 恢复；精确继续缺少计划时明确失败 |
| `submit_human_response(run_id, response)` | 两者必填 | `LoopResult` | 仅提交并持久化，不自动恢复；相同幂等键和相同语义为 no-op |
| `cancel(run_id)` | 非空 run id | `bool` | 登记协作式取消；首次登记为真，未知 run id 也可被预取消 |

## 请求、计划与预算字段

### `LoopLimits`

| 字段 | 类型 | 必填 | 默认 | 业务含义 | 校验不变量 | 安全与持久化 |
|---|---|---:|---|---|---|---|
| `max_cycles` | `int` | 否 | `5` | 最大规划轮次 | 至少 1 | checkpoint v2 持久化；不限制模型调用次数 |
| `max_attempts` | `int` | 否 | `20` | Executor 总调用上限 | 至少 1 | 重试会继续消耗，暂停恢复不重置 |
| `max_steps_per_plan` | `int` | 否 | `20` | 单计划最大步骤数 | 至少 1 | 超出时阻塞为 `STEP_LIMIT`，不执行计划 |
| `timeout_seconds` | `float \| None` | 否 | `None` | 活跃执行累计超时 | 非空时必须大于 0 | 人工等待不计时；重试等待、组件和持久化耗时计入 |

### `LoopRequest`

| 字段 | 类型 | 必填 | 默认 | 业务含义 | 校验不变量 | 安全与持久化 |
|---|---|---:|---|---|---|---|
| `goal` | `str` | 是 | 无 | 整体目标 | 去除空白后非空 | 会进入 Planner 和检查点，可能敏感 |
| `acceptance_criteria` | `tuple[str, ...]` | 否 | `()` | 整体目标验收条件 | 不允许空白项 | 不会自动执行内容安全检查 |
| `limits` | `LoopLimits` | 否 | 默认对象 | 运行边界 | 见上表 | 暂停恢复沿用原值 |
| `metadata` | `Mapping[str, object]` | 否 | `{}` | 关联和审计扩展值 | 复制并冻结顶层映射 | checkpoint 要求完整值可 JSON 序列化；不脱敏 |

### `PlanStep` 与 `Plan`

| DTO.字段 | 类型 | 必填 | 默认 | 业务含义 | 校验不变量 | 安全与持久化 |
|---|---|---:|---|---|---|---|
| `PlanStep.description` | `str` | 是 | 无 | Executor 的工作说明 | 去除空白后非空 | 进入检查点和事件 |
| `PlanStep.executor` | `str` | 否 | `"default"` | Executor 注册名 | 去除空白后非空 | 未注册会抛 `ComponentNotFoundError` |
| `PlanStep.acceptance_criteria` | `tuple[str, ...]` | 否 | `()` | 本步骤验收条件 | 不允许空白项 | Verifier 负责业务解释 |
| `PlanStep.requires_approval` | `bool` | 否 | `False` | 执行前是否走审批门 | 无额外校验 | 只有真值才会触发审批 |
| `PlanStep.step_id` | `str` | 否 | 随机 UUID hex | 审计、审批和恢复关联键 | 去除空白后非空；同一计划必须唯一 | 不应复用为授权凭据 |
| `Plan.steps` | `tuple[PlanStep, ...]` | 是 | 无 | 有序步骤 | DTO 本身允许空；控制器拒绝空计划和重复 step id | 顺序决定执行和恢复游标 |

## 执行、验证与审计字段

| DTO.字段 | 类型 | 必填 | 默认 | 业务含义 | 校验不变量 | 安全与持久化 |
|---|---|---:|---|---|---|---|
| `ArtifactRef.name` | `str` | 是 | 无 | 制品名称 | 非空 | 进入审计结果 |
| `ArtifactRef.uri` | `str` | 是 | 无 | 外部制品位置 | 非空 | Core 不验证协议、权限或可访问性，不应内嵌凭据 |
| `ArtifactRef.media_type` | `str \| None` | 否 | `None` | IANA 媒体类型 | 非空值不得为空白 | Core 不验证实际内容 |
| `ArtifactRef.metadata` | `Mapping[str, object]` | 否 | `{}` | 制品扩展信息 | 顶层复制冻结 | checkpoint 要求 JSON 值，不脱敏 |
| `ExecutionResult.output` | `str` | 是 | 无 | Executor 文本输出 | 允许空字符串 | 会成为公开结果的最终 output，可能敏感 |
| `ExecutionResult.artifacts` | `tuple[ArtifactRef, ...]` | 否 | `()` | 外部制品引用 | 无额外校验 | 不应把大制品内容塞入 checkpoint |
| `ExecutionResult.metadata` | `Mapping[str, object]` | 否 | `{}` | 执行扩展信息 | 顶层复制冻结 | checkpoint 要求 JSON 值 |
| `VerificationResult.passed` | `bool` | 是 | 无 | 步骤是否通过 | passed 为真时 `failed_criteria` 必须为空 | 不是整体目标完成结论 |
| `VerificationResult.feedback` | `str` | 否 | `""` | 后续规划反馈 | 允许空 | 失败时进入下一轮 Planner 上下文 |
| `VerificationResult.score` | `float \| None` | 否 | `None` | 统一 0–100 评分 | 非空时在 0–100 | 仅供业务解释 |
| `VerificationResult.evidence` | `tuple[str, ...]` | 否 | `()` | 支持结论的证据 | 不允许空白项 | Core 不验证证据真实性 |
| `VerificationResult.failed_criteria` | `tuple[str, ...]` | 否 | `()` | 未通过条件 | 不允许空白项 | passed 为真时必须为空 |
| `IterationRecord.cycle` | `int` | 是 | 无 | 规划轮次 | 至少 1 | checkpoint 审计字段 |
| `IterationRecord.step_index` | `int` | 是 | 无 | 零基步骤索引 | 不得为负 | checkpoint 审计字段 |
| `IterationRecord.step` | `PlanStep` | 是 | 无 | 步骤快照 | 继承步骤不变量 | 不随后续注册表变化 |
| `IterationRecord.execution` | `ExecutionResult` | 是 | 无 | 执行证据 | 继承执行不变量 | 明文持久化 |
| `IterationRecord.verification` | `VerificationResult` | 是 | 无 | 验证证据 | 继承验证不变量 | 明文持久化 |
| `IterationRecord.attempt` | `int` | 否 | `1` | 当前步骤内部尝试序号 | 至少 1 | 与 `LoopContext.total_attempts` 口径不同 |

`completed_steps` 在形成一条执行和验证记录后递增，即使该验证失败并触发重新规划；展示层不要
把它解释为“通过验证的步骤数”。公开 `LoopResult.output` 取最后一条记录的 execution output，
不是所有步骤输出的自动聚合。

## 人工交互字段与动作

### 字段

| DTO.字段 | 类型 | 必填 | 默认 | 业务含义 | 校验不变量 | 安全与持久化 |
|---|---|---:|---|---|---|---|
| `HumanInteractionRequest.kind` | `HumanInteractionKind` | 是 | 无 | 审批、输入或整体完成复核 | 当前不额外检查运行时类型 | checkpoint v2 持久化 |
| `HumanInteractionRequest.prompt` | `str` | 是 | 无 | 展示给人的问题 | 去除空白后非空 | 可能含目标和步骤内容，应按权限展示 |
| `HumanInteractionRequest.allowed_actions` | `tuple[HumanAction, ...]` | 否 | 四种动作 | 本次允许的动作 | 非空且不可重复 | 服务端提交时再次校验 |
| `HumanInteractionRequest.interaction_id` | `str` | 否 | UUID hex | 请求关联键 | 非空 | 必须与 response 精确匹配 |
| `HumanInteractionRequest.step_id` | `str \| None` | 否 | `None` | 可选步骤关联 | 非空值不得为空白 | 审批通过时写入已批准步骤集合 |
| `HumanInteractionRequest.metadata` | `Mapping[str, object]` | 否 | `{}` | UI 和审计扩展 | 顶层复制冻结 | JSON checkpoint 明文保存 |
| `HumanInteractionRequest.created_at` | `datetime` | 否 | 当前 UTC | 请求时间 | 必须带时区 | checkpoint ISO 时间 |
| `HumanResponse.interaction_id` | `str` | 是 | 无 | 响应关联键 | 非空且必须匹配 pending request | 不得由客户端猜测其他运行 ID |
| `HumanResponse.action` | `HumanAction` | 是 | 无 | 标准人工动作 | 必须在 allowed actions 中 | 决定继续、阻塞或重规划 |
| `HumanResponse.content` | `str` | 否 | `""` | 意见、原因或补充输入 | `REVISE/PROVIDE_INPUT` 时必须非空 | 会进入反馈历史和后续 Planner，可能敏感 |
| `HumanResponse.idempotency_key` | `str` | 否 | UUID hex | 安全重试键 | 非空；相同键不同语义冲突 | 应由调用方持久化并在重试时复用 |
| `HumanResponse.metadata` | `Mapping[str, object]` | 否 | `{}` | 操作者和审计扩展 | 顶层复制冻结 | 不应保存认证令牌 |
| `HumanResponse.responded_at` | `datetime` | 否 | 当前 UTC | 响应时间 | 必须带时区 | checkpoint ISO 时间 |
| `HumanInteractionRecord.request` | `HumanInteractionRequest` | 是 | 无 | 原始请求 | interaction id 必须与 response 相同 | 完整历史持久化 |
| `HumanInteractionRecord.response` | `HumanResponse` | 是 | 无 | 已提交响应 | 同上 | 完整历史持久化 |
| `HumanInteractionRecord.recorded_at` | `datetime` | 否 | 当前 UTC | Core 记录时间 | 必须带时区 | checkpoint ISO 时间 |

### 动作语义

| 动作 | 提交后的状态与恢复语义 |
|---|---|
| `APPROVE` | 记录步骤或整体完成批准；随后 `CONTINUE` 精确继续，审批门不重复调用 |
| `REJECT` | 进入 `BLOCKED/HUMAN_REJECTED`；`CONTINUE` 被拒绝，需显式 `REPLAN` 才可恢复 |
| `REVISE` | content 必填，保留历史并设置强制重新规划 |
| `PROVIDE_INPUT` | content 必填，保留历史并设置强制重新规划 |

## 上下文与公开结果字段

### `LoopContext`

`LoopContext` 是控制器内部可变状态，但作为公共协议参数和 checkpoint 载体公开。扩展组件应读取
传入的 snapshot，不应修改它并期待影响控制器。

| 字段 | 类型 | 默认 | 业务含义与持久化不变量 |
|---|---|---|---|
| `request` | `LoopRequest` | 必填 | 原始请求，完整持久化 |
| `run_id` | `str` | UUID hex | 运行主键 |
| `status` | `LoopStatus` | `CREATED` | 当前状态，转换受 `ensure_transition` 约束 |
| `records` | `list[IterationRecord]` | `[]` | 执行验证审计历史；snapshot 复制列表 |
| `feedback` | `str` | `""` | 最近反馈，不等同于完整人工历史 |
| `current_plan` | `Plan \| None` | `None` | 暂停恢复所需计划 |
| `current_step_index` | `int` | `0` | 下一步或当前待执行的零基游标 |
| `cycle_count` | `int` | `0` | 已开始的规划轮次 |
| `total_attempts` | `int` | `0` | Executor 总调用次数 |
| `completed_steps` | `int` | `0` | 已形成 record 的步骤数，包括验证失败记录 |
| `stop_reason` | `StopReason \| None` | `None` | 结构化停止原因 |
| `error` | `str` | `""` | 组件错误摘要；可能包含原异常文本，持久化前应脱敏 |
| `pending_interaction` | `HumanInteractionRequest \| None` | `None` | 当前唯一待处理人工请求 |
| `human_interactions` | `list[HumanInteractionRecord]` | `[]` | 完整已处理反馈历史 |
| `approved_step_ids` | `set[str]` | `set()` | 恢复时避免重复审批 |
| `replan_required` | `bool` | `False` | 人工意见是否强制重新规划 |
| `completion_approved` | `bool` | `False` | 整体人工复核是否通过 |
| `event_sequence` | `int` | `0` | 单运行单调事件序号 |
| `revision` | `int` | `0` | Checkpoint CAS revision |
| `active_elapsed_seconds` | `float` | `0` | 已结算活跃耗时 |
| `active_started_at` | `datetime \| None` | `None` | 当前活跃计时段起点 |
| `started_at` | `datetime` | 当前 UTC | 运行创建时间 |
| `updated_at` | `datetime` | 当前 UTC | 最近状态提交时间 |

### `LoopResult`

| 字段 | 类型 | 必填 | 默认 | 业务含义 | 安全与持久化 |
|---|---|---:|---|---|---|
| `run_id` | `str` | 是 | 无 | 运行标识 | 公开关联键 |
| `status` | `LoopStatus` | 是 | 无 | 返回时状态 | 不保证一定终态，可能暂停或阻塞 |
| `output` | `str` | 是 | 无 | 最后一条执行输出 | 不脱敏 |
| `cycles` | `int` | 是 | 无 | 规划轮次 | 预算审计 |
| `total_attempts` | `int` | 是 | 无 | Executor 调用数 | 预算审计 |
| `completed_steps` | `int` | 是 | 无 | record 数口径的步骤进度 | 可能包含失败验证 |
| `records` | `tuple[IterationRecord, ...]` | 是 | 无 | 不可变审计轨迹 | 可能含敏感输出和证据 |
| `stop_reason` | `StopReason \| None` | 是 | 无 | 结构化原因 | 上层应优先使用它而非解析 error |
| `error` | `str` | 否 | `""` | 错误摘要 | 可能包含异常文本 |
| `pending_interaction` | `HumanInteractionRequest \| None` | 否 | `None` | 待处理人工请求 | 对外展示前必须授权 |
| `human_interactions` | `tuple[HumanInteractionRecord, ...]` | 否 | `()` | 完整反馈历史 | 可能含身份和业务意见 |
| `revision` | `int` | 否 | `0` | 最新 checkpoint revision | 可用于诊断并发，不替代写入 CAS |
| `event_sequence` | `int` | 否 | `0` | 最新事件序号 | 可用于审计缺口检测 |

`iterations` 是 `len(records)` 的只读兼容属性，`feedback_history` 返回 `human_interactions`。

## 决策、组件规格与事件字段

### 决策枚举

| 枚举 | 稳定值 | 控制器语义 |
|---|---|---|
| `ApprovalDecision` | `APPROVED`、`REJECTED`、`DEFERRED` | 继续执行、直接阻塞、创建人工审批请求 |
| `RetryAction` | `RETRY`、`REPLAN`、`FAIL` | 重试 Executor、结束当前计划并重规划、标记失败并重抛 |
| `CompletionAction` | `ACCEPT`、`REPLAN`、`REQUEST_HUMAN`、`STOP` | 完成、重规划、暂停等待人、以 `COMPLETION_REJECTED` 阻塞 |
| `HumanInteractionKind` | `APPROVAL`、`INPUT`、`COMPLETION_REVIEW` | 步骤审批、补充输入、整体完成复核 |
| `ResumeMode` | `CONTINUE`、`REPLAN` | 精确继续当前计划、丢弃当前计划并重新规划 |

| DTO.字段 | 类型 | 必填 | 默认 | 不变量与语义 |
|---|---|---:|---|---|
| `RetryDecision.action` | `RetryAction` | 是 | 无 | `RETRY`、`REPLAN` 或 `FAIL` |
| `RetryDecision.delay_seconds` | `float` | 否 | `0` | 不得为负；等待计入活跃 timeout |
| `CompletionDecision.action` | `CompletionAction` | 是 | 无 | `ACCEPT`、`REPLAN`、`REQUEST_HUMAN`、`STOP` |
| `CompletionDecision.feedback` | `str` | 否 | `""` | 传给后续规划或结果 |
| `CompletionDecision.interaction` | `HumanInteractionRequest \| None` | 否 | `None` | 仅 `REQUEST_HUMAN` 必须且允许携带 |
| `ComponentSpec.name` | `str` | 是 | 无 | 非空稳定注册名 |
| `ComponentSpec.factory` | `Callable[[], T]` | 是 | 无 | repr 隐藏；无参创建实例 |
| `ComponentSpec.version` | `str` | 否 | `"0.1.0"` | 非空组件版本 |
| `ComponentSpec.capabilities` | `frozenset[str]` | 否 | 空集合 | 项目去空白且不得为空字符串 |
| `ComponentSpec.description` | `str` | 否 | `""` | 人类说明 |
| `ComponentSpec.metadata` | `Mapping[str, str]` | 否 | `{}` | 顶层复制冻结；不要存凭据 |
| `PluginDefinition.name` | `str` | 是 | 无 | 非空插件名 |
| `PluginDefinition.version` | `str` | 是 | 无 | 非空插件版本 |
| `PluginDefinition.components` | `tuple[ComponentSpec, ...]` | 是 | 无 | 至少一个且名称不可重复 |
| `LoopEvent.event_type` | `LoopEventType` | 是 | 无 | 生命周期事件名 |
| `LoopEvent.context` | `LoopContext` | 是 | 无 | 控制器传入隔离 snapshot |
| `LoopEvent.occurred_at` | `datetime` | 否 | 当前 UTC | 当前 DTO 不额外校验时区 |
| `LoopEvent.detail` | `str` | 否 | `""` | 简短扩展详情 |
| `LoopEvent.sequence` | `int` | 否 | `0` | 控制器发布时为单运行单调序号 |

## 扩展协议方法

| 协议 | 方法 | 业务契约 |
|---|---|---|
| `Planner` | `await plan(context) -> Plan` | 基于 snapshot 创建有序计划；空计划和重复 step id 会失败 |
| `Executor` | `await execute(step, context) -> ExecutionResult` | 执行但不判断正确性；普通异常交给 RetryPolicy |
| `Verifier` | `await verify(step, result, context) -> VerificationResult` | 独立验证步骤；失败触发下一 cycle |
| `CheckpointStore` | `await save(context, expected_revision=None) -> int`、`await load(run_id)` | save 必须原子 CAS 且返回严格递增 revision |
| `LoopPolicy` | `can_continue(context) -> bool` | 每个安全边界执行，不能承担异步 I/O |
| `EventPublisher` | `await publish(event)` | 负责传输或持久化；Core 不重试、不去重 |
| `ApprovalGate` | `await decide(step, context) -> ApprovalDecision` | `DEFERRED` 创建人工请求，`REJECTED` 阻塞 |
| `RetryPolicy` | `decide(error, attempt, context) -> RetryDecision` | `attempt` 为当前步骤内部从 1 开始的尝试序号 |
| `CompletionEvaluator` | `await evaluate(context) -> CompletionDecision` | 全部步骤通过后验收整体目标 |

所有协议均为 `runtime_checkable Protocol`，实现无需继承。类型结构检查不能验证业务原子性、
异步行为或返回值不变量，这些仍由实现者和契约测试保证。

## 状态、停止原因与事件

### 状态

| 状态 | 含义 | 是否 `is_terminal` | 是否可由 `resume` 恢复 |
|---|---|---:|---:|
| `CREATED` | 已创建尚未规划 | 否 | 否 |
| `PLANNING` | 正在规划 | 否 | 否 |
| `WAITING_APPROVAL` | 审批门处理中 | 否 | 否 |
| `EXECUTING` | 正在执行 | 否 | 否 |
| `VERIFYING` | 正在验证 | 否 | 否 |
| `PAUSED` | 等待外部人工响应 | 否 | 是，需先提交响应 |
| `BLOCKED` | 策略、预算、限制或拒绝导致阻塞 | 否 | 是，具体原因可能要求 `REPLAN` |
| `COMPLETED` | 已完成 | 是 | 否 |
| `CANCELLED` | 已在安全边界取消 | 是 | 否 |
| `TIMED_OUT` | 活跃时间耗尽 | 是 | 否 |
| `FAILED` | 未处理组件异常 | 是 | 否 |

`StopReason` 的稳定值为：`COMPLETED`、`POLICY_REJECTED`、`APPROVAL_REJECTED`、
`APPROVAL_DEFERRED`、`HUMAN_INPUT_REQUIRED`、`HUMAN_REJECTED`、`COMPLETION_REJECTED`、
`CYCLE_LIMIT`、`ATTEMPT_LIMIT`、`STEP_LIMIT`、`CANCELLED`、`TIMED_OUT`、
`COMPONENT_ERROR`、`BUDGET_EXHAUSTED`。

### 事件

| 阶段 | `LoopEventType` |
|---|---|
| Loop | `LOOP_STARTED`、`LOOP_RESUMED`、`LOOP_PAUSED`、`LOOP_COMPLETED`、`LOOP_BLOCKED`、`LOOP_CANCELLED`、`LOOP_TIMED_OUT`、`LOOP_FAILED` |
| 规划与执行 | `PLANNING_STARTED`、`PLAN_CREATED`、`EXECUTION_STARTED`、`VERIFICATION_STARTED`、`ITERATION_COMPLETED`、`COMPONENT_RETRYING` |
| 审批与人工 | `APPROVAL_REQUESTED`、`APPROVAL_GRANTED`、`HUMAN_INTERACTION_REQUESTED`、`HUMAN_RESPONSE_SUBMITTED`、`HUMAN_APPROVED`、`HUMAN_REJECTED`、`HUMAN_REVISED`、`HUMAN_INPUT_PROVIDED` |
| 整体验收 | `COMPLETION_EVALUATION_STARTED`、`COMPLETION_REPLAN_REQUESTED` |

`LocalEventPublisher` 按订阅顺序串行执行同步或异步 handler，并对订阅列表取稳定快照。重复订阅
会被忽略；handler 异常会停止后续 handler 并向控制器传播，没有隔离、重试或持久化能力。

## 错误语义

| 异常 | 触发条件与调用方动作 |
|---|---|
| `ComponentNotFoundError` | 请求的注册组件不存在；修正装配或名称 |
| `ComponentAlreadyRegisteredError` | 未显式 `replace=True` 时重复注册 |
| `InvalidPluginError` | Entry Point 未返回合法 `PluginDefinition` |
| `InvalidPlanError` | Planner 返回空计划或重复 step id；运行标记 FAILED 后异常重抛 |
| `CheckpointSchemaError` | checkpoint 版本、字段、时间或 JSON 值非法；不得静默降级 |
| `CheckpointConflictError` | CAS revision 冲突或实现未递增 revision；重新加载，不得覆盖 |
| `InvalidStateTransitionError` | 非法状态转换；视为编排或集成错误 |
| `LoopNotFoundError` | 恢复或提交反馈时没有 checkpoint |
| `LoopNotResumableError` | 状态、pending interaction 或计划不允许恢复 |
| `HumanInteractionNotPendingError` | 没有待处理请求、interaction id/action 不匹配 |
| `HumanResponseConflictError` | 相同幂等键携带不同 interaction/action/content/metadata |
| `ResourceLimitExceededError` | 本地额度不足；Core 映射为 `BLOCKED/BUDGET_EXHAUSTED`，不调用 RetryPolicy |

除额度异常外，未处理异常会写入 `FAILED/COMPONENT_ERROR` checkpoint、发布失败事件、记录日志，
然后向调用方重抛。错误字符串由“异常类型 + 原异常文本”组成，供应商适配器和业务组件必须先
完成敏感信息清洗。

## 检查点、并发与事件一致性

- `LoopCheckpointCodec.schema_version` 固定为 `2`，不读取 v1，也没有兼容 shim。
- v2 保存请求、计划、records、人工请求与历史、审批集合、恢复标志、事件序号、revision、
  活跃计时和带时区时间。`dumps` 禁止 NaN/Infinity 和非 JSON metadata。
- 每次状态提交先增加 event sequence，再用旧 revision 调用 `CheckpointStore.save`；存储必须把
  revision 严格加 1。CAS 冲突直接传播，防止双重 resume 或并发反馈覆盖胜者。
- 状态保存和外部事件发布不是分布式事务：checkpoint 已成功但 publisher 失败时可能出现事件
  缺口。企业 Publisher 应使用幂等键 `(run_id, sequence)`，并通过 outbox 或补偿读取实现可靠投递。
- `submit_human_response` 在 CAS 冲突后会重新读取；若相同幂等响应已经提交，则返回最新结果。
- `ComponentRegistry` 和 `FactoryCatalog` 用线程锁保护名称映射及原子批量更新，但不跟踪活动调用、
  不调用 `start/aclose`。需要无中断资源替换时使用 RuntimeContainer 或模型注册表租约。

## 企业安全与当前限制

- Core 不做认证、授权、租户隔离、内容审核、提示注入防护、加密、脱敏或密钥管理。
- goal、criteria、metadata、输出、制品 URI、验证证据、人工反馈和 error 都可能进入 checkpoint 与
  事件。持久化和事件后端必须采用最小权限、传输/静态加密、保留期限和删除审计。
- `MappingProxyType` 只冻结顶层映射，调用方仍应传入 JSON 值或不可变嵌套结构。
- timeout 是协作边界，不是隔离机制；被调用组件必须正确响应 asyncio 取消。同步阻塞代码、线程、
  子进程或远程任务可能继续运行，应由对应 Runtime/Sandbox 实现终止策略。
- Core 仅对 Executor 提供 RetryPolicy；Planner、Verifier、审批、整体验收、checkpoint 和事件的重试
  应由各自适配器或上层可靠性机制处理。
- 本包不包含模型、Agent 默认实现、工具、默认策略、持久化 checkpoint、队列 Worker、HTTP API
  或真正的代码沙箱。
