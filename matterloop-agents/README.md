# matterloop-agents

> 跨模块生产拓扑、资源所有权与上线检查见[企业集成指南](../docs/enterprise-integration.md)。

`matterloop-agents` 提供标准单 Agent 组件，以及基于中心控制器、任务 DAG 和能力路由的 TeamLoop。
组件只保存 `ModelRegistry`、`ToolRegistry` 或结构协议，不构造供应商 SDK、不读取环境变量，也不
隐式选择模型。OpenAI、DeepSeek、MiniMax、千问和智谱等客户端由应用从
`matterloop_models.providers` 构造并注册。

## 单 Agent 组件

| 组件 | 配置必填 | 真实默认 |
| --- | --- | --- |
| `ModelPlanner` | `model` | `default_executor="default"`、`max_steps=20`、`max_output_tokens=4096`、`memory_namespace="default"`、`memory_limit=5` |
| `ToolCallingWorker` | `model` | `tool_names=()`、`max_tool_rounds=8`、`max_output_tokens=4096` |
| `CriteriaVerifier` | `model` | `pass_score=80`、`max_output_tokens=2048` |
| `ModelReviewer` | `model` | `max_output_tokens=3072` |

对应配置和通用审查结果的完整字段如下：

| DTO.字段 | 类型 | 必填 | 默认 | 业务含义 | 校验不变量 | 安全与持久化 |
| --- | --- | ---: | --- | --- | --- | --- |
| `ModelPlannerConfig.model` | `str` | 是 | 无 | ModelRegistry 注册名 | 去除空白后非空 | 不能保存供应商密钥 |
| `ModelPlannerConfig.default_executor` | `str` | 否 | `"default"` | 模型未指定时的执行器名 | 去除空白后非空 | 必须在 Core Executor 注册表存在 |
| `ModelPlannerConfig.max_steps` | `int` | 否 | `20` | 模型计划步骤硬上限 | 至少 1 | 与 Core `max_steps_per_plan` 同时生效 |
| `ModelPlannerConfig.max_output_tokens` | `int` | 否 | `4096` | 单次规划最大输出 | 至少 1 | 不是总 Token 预算 |
| `ModelPlannerConfig.memory_namespace` | `str` | 否 | `"default"` | 长期记忆检索命名空间 | 去除空白后非空 | 企业环境应包含租户隔离维度 |
| `ModelPlannerConfig.memory_limit` | `int` | 否 | `5` | 最多注入的记忆条数 | 至少 1 | 记忆正文会进入模型请求 |
| `ToolCallingWorkerConfig.model` | `str` | 是 | 无 | Worker 模型注册名 | 去除空白后非空 | 不包含凭据 |
| `ToolCallingWorkerConfig.tool_names` | `tuple[str, ...]` | 否 | `()` | 模型可见的工具 allowlist | 非空且不得重复 | 仍需 ToolAuthorizer 二次授权 |
| `ToolCallingWorkerConfig.max_tool_rounds` | `int` | 否 | `8` | 单步骤工具续轮上限 | 至少 1 | 超限抛类型化异常 |
| `ToolCallingWorkerConfig.max_output_tokens` | `int` | 否 | `4096` | 每轮模型输出上限 | 至少 1 | 每轮都计入 UsageLedger |
| `CriteriaVerifierConfig.model` | `str` | 是 | 无 | 验证模型注册名 | 去除空白后非空 | 应与执行 Agent 独立配置 |
| `CriteriaVerifierConfig.pass_score` | `float` | 否 | `80.0` | 通过最低分 | 0–100 | 模型声明通过仍必须达到阈值 |
| `CriteriaVerifierConfig.max_output_tokens` | `int` | 否 | `2048` | 验证响应上限 | 至少 1 | 进入模型用量统计 |
| `ModelReviewerConfig.model` | `str` | 是 | 无 | 审查模型注册名 | 去除空白后非空 | 不包含凭据 |
| `ModelReviewerConfig.max_output_tokens` | `int` | 否 | `3072` | 审查响应上限 | 至少 1 | 进入模型用量统计 |
| `ReviewResult.score` | `float` | 是 | 无 | 综合质量分 | 由解析器限制为 0–100 | 不是 Core 最终状态 |
| `ReviewResult.summary` | `str` | 是 | 无 | 审查摘要 | 必须由模型 Schema 返回 | 可能包含业务数据 |
| `ReviewResult.evidence` | `tuple[str, ...]` | 否 | `()` | 支持结论的证据 | 本 DTO 不再次验证真实性 | 可能进入后续验证结果 |
| `ReviewResult.issues` | `tuple[str, ...]` | 否 | `()` | 发现的问题 | 非空集合会导致适配器验证失败 | 会成为失败条件 |
| `ReviewResult.recommendations` | `tuple[str, ...]` | 否 | `()` | 可执行建议 | 无额外校验 | 由调用方决定是否持久化 |

```python
from matterloop_agents import ModelPlanner, ModelPlannerConfig

planner = ModelPlanner(
    models=model_registry,
    config=ModelPlannerConfig(model="planner"),
)
```

Planner、Worker、Verifier 与 Reviewer 都在每次模型事务开始时通过 `ModelRegistry.acquire()` 取得
租约。模型热替换只影响新事务；已开始的工具续轮继续使用原客户端，直到租约释放。模型结构化
输出和工具参数仍在本地解析，解析失败、计划超限、非法工具和工具循环超限会抛类型化 Agent 异常。

## TeamLoop 架构

协作 API 位于 `matterloop_agents.collaboration`，不增加独立发行包：

```text
TeamRequest
  -> TeamPlanner 根据能力快照、历史审查和人工反馈生成 TaskSpec DAG
  -> TeamOrchestrator 校验图并找出 READY 任务
  -> TeamApprovalGate 只处理 requires_approval=True 的任务
  -> AgentDirectory + AgentSelectionPolicy 按能力和容量取得 Agent 租约
  -> 多个 AgentEndpoint 并行执行
  -> TaskVerifier 独立验证；失败先消耗任务尝试预算
  -> ResultAggregator 生成团队草稿
  -> TeamReviewer 执行整体目标验收
  -> ACCEPT / REPLAN / REQUEST_HUMAN / STOP
  -> TeamRepository 以版本 CAS 保存完整快照，并提供运行级控制器租约
  -> TeamEventPublisher 发布生命周期事件
```

`TeamOrchestrator` 是团队快照的唯一写入者。并行 Agent 只收到隔离的 `AgentTaskContext` 并返回
`TaskResult`；结果在批次汇合后由控制器顺序提交，Agent 不能绕过控制器修改全局任务状态。
`dependency_results` 是 DAG 依赖之间的正式通信路径；Mailbox 与 ArtifactStore 是可选扩展，不会
自动修改 TaskGraph。

### 协作 DTO 完整字段矩阵

下表覆盖协作层全部集成字段。字段组仍按同一 DTO 的构造顺序排列；动态时间均为带时区 UTC，
动态 ID 均为 UUID hex。

| DTO.字段 | 类型 | 必填 | 默认 | 业务含义 | 校验不变量 | 安全与持久化 |
| --- | --- | ---: | --- | --- | --- | --- |
| `TeamRequest.goal` / `TeamRequest.acceptance_criteria` / `TeamRequest.limits` / `TeamRequest.metadata` | `str` / `tuple[str, ...]` / `TeamLimits` / `Mapping` | goal 是 | `()` / 默认 limits / `{}` | 团队总体输入 | 目标和条件不得为空白 | metadata 递归冻结但不脱敏 |
| `TeamLimits.max_tasks` / `max_concurrency` / `max_task_attempts` | `int` | 否 | `50` / `4` / `3` | 单计划、并发和任务尝试上限 | 均至少 1 | 暂停恢复不重置 |
| `TeamLimits.max_cycles` / `max_plan_revisions` / `timeout_seconds` | `int` / `int` / `float \| None` | 否 | `3` / `2` / `None` | 外层循环、重规划和活跃超时 | cycle 至少 1；revision 不得为负；超时必须为正 | 人工等待不计时 |
| `AgentSpec.agent_id` / `capabilities` | `str` / `frozenset[str]` | 是 | 无 | Agent 路由标识和能力 | 均非空 | 会进入目录快照和事件 |
| `AgentSpec.max_concurrency` / `version` / `description` / `role` / `metadata` | 混合 | 否 | `1` / `"0.1.0"` / `""` / `"worker"` / `{}` | 并发、版本、展示和扩展信息 | 并发至少 1，version/role 非空 | metadata 递归冻结但不脱敏 |
| `TaskSpec.task_id` / `description` / `capability` | `str` | 是 | 无 | DAG 节点、工作说明和能力约束 | 均非空；task_id 在计划内唯一 | 完整进入 Snapshot |
| `TaskSpec.dependencies` / `acceptance_criteria` / `requires_approval` / `priority` / `metadata` | 混合 | 否 | `()` / `()` / `False` / `0` / `{}` | 依赖、验收、审批和调度 | 依赖不重复、不指向自身；条件不得为空白 | metadata 明文进入快照 |
| `AgentTaskContext.team_run_id` / `request` / `task` / `agent_id` / `attempt` | 混合 | 是 | 无 | 单次 Agent 调用的归属与尝试 | 标识非空；attempt 至少 1 | 是 Agent 正式输入 |
| `AgentTaskContext.dependency_results` / `previous_error` / `human_feedback` | 混合 | 否 | `()` / `""` / `()` | 上游结果、重试反馈和人工历史 | 只能由控制器构造 | 可能含敏感输出与人工意见 |
| `TaskResult.task_id` / `agent_id` / `success` | 混合 | 是 | 无 | Agent 返回结果归属 | 标识非空 | 控制器会校验任务归属 |
| `TaskResult.output` / `artifacts` / `error` / `attempt` / `metadata` | 混合 | 否 | `""` / `()` / `""` / `1` / `{}` | 输出、制品、错误与计量上下文 | 成功结果不能带 error；attempt 至少 1 | 完整结果可能进入事件 |
| `TaskVerification.passed` / `feedback` / `score` / `evidence` / `failed_criteria` | 混合 | passed 是 | `""` / `None` / `()` / `()` | 独立任务验证 | score 0–100；passed 时失败条件必须为空 | 明文持久化 |
| `TaskState.spec` / `status` / `attempt` / `approval_granted` | 混合 | spec 是 | `PENDING` / `0` / `False` | DAG 节点当前状态 | attempt 不得为负 | TeamRepository 持久化 |
| `TaskState.assigned_agent` / `result` / `verification` / `error` | 混合 | 否 | `None` / `None` / `None` / `""` | 分配、结果和验证快照 | RUNNING/VERIFYING/SUCCEEDED 有严格组合不变量 | 恢复时用于避免重放副作用 |
| `TeamPlanningContext.run_id` / `request` / `cycle` / `plan_revision` / `available_agents` | 混合 | 是 | 无 | Planner 的稳定运行与能力快照 | run_id 非空；cycle 至少 1；revision 不得为负 | 不包含活跃端点实例 |
| `TeamPlanningContext.prior_reviews` / `human_feedback` | 混合 | 否 | `()` / `()` | 历史审查和人工输入 | 由控制器按顺序提供 | 会进入 Planner 模型请求 |
| `TeamReviewContext.run_id` / `request` / `cycle` / `plan_revision` / `task_results` / `draft_output` | 混合 | 是 | 无 | Reviewer 的整体草稿输入 | 计数与标识同规划上下文 | 可能包含全部任务输出 |
| `TeamReviewContext.prior_reviews` / `human_feedback` | 混合 | 否 | `()` / `()` | 前序审查与人工历史 | 只读历史 | 可能敏感 |
| `TeamReview.action` / `feedback` / `score` / `evidence` / `failed_criteria` / `interaction` | 混合 | action 是 | `""` / `None` / `()` / `()` / `None` | 团队整体验收及后续动作 | REQUEST_HUMAN 必须带 interaction，其他动作禁止携带 | 进入 cycle history 和事件 |
| `TeamCycleRecord.cycle` / `plan_revision` / `tasks` | 混合 | 是 | 无 | 一轮计划和最终任务状态 | cycle 至少 1；revision 不得为负 | 永久审计证据 |
| `TeamCycleRecord.draft_output` / `review` / `error` | 混合 | 否 | `""` / `None` / `""` | 草稿、审查和轮次错误 | 无额外校验 | 可能含模型输出 |
| `TeamSnapshot.request` / `tasks` | 混合 | 是 | 无 | 可恢复团队完整输入与图状态 | task_id 不得重复 | Repository 的 CAS 主体 |
| `TeamSnapshot.run_id` / `status` / `version` / `stop_reason` / `output` / `error` | 混合 | 否 | UUID / `CREATED` / `0` / `None` / `""` / `""` | 运行身份和对外状态 | version 不得为负 | 事件会携带完整快照 |
| `TeamSnapshot.cycle` / `plan_revision` / `cycle_history` | 混合 | 否 | `0` / `0` / `()` | 外层循环和历史 | 计数不得为负 | 恢复和审计使用 |
| `TeamSnapshot.pending_interaction` / `pending_review` / `human_interactions` / `review_approved_cycle` | 混合 | 否 | `None` / `None` / `()` / `None` | HITL 与审查恢复点 | approved cycle 非空时至少 1 | 含人工意见，必须限制访问 |
| `TeamSnapshot.active_elapsed_seconds` / `active_started_at` / `created_at` / `updated_at` | 混合 | 否 | `0.0` / `None` / 当前 UTC / 当前 UTC | 活跃计时和审计时间 | elapsed 不得为负，时间带时区 | 人工等待期间暂停累计 |
| `TeamResult.run_id` / `status` / `task_results` | 混合 | 是 | 无 | 对外不可变运行投影 | run_id 非空 | 只包含通过任务结果 |
| `TeamResult.output` / `stop_reason` / `error` / `cycle` / `cycle_history` | 混合 | 否 | `""` / `None` / `""` / `0` / `()` | 输出、停止和循环历史 | 由控制器构造 | 可能含敏感业务数据 |
| `TeamResult.pending_interaction` / `human_interactions` / `started_at` / `finished_at` | 混合 | 否 | `None` / `()` / `None` / `None` | HITL 和运行时间 | 暂停结果可能没有 finished_at | API 展示前必须授权 |
| `TeamEvent.event_type` / `snapshot` | 混合 | 是 | 无 | 生命周期动作和当时快照 | snapshot 不可变 | 事件可能非常大且敏感 |
| `TeamEvent.detail` / `metadata` / `occurred_at` | 混合 | 否 | `""` / `{}` / 当前 UTC | 诊断和扩展信息 | 时间必须带时区 | Publisher 不自动脱敏 |
| `AgentMessage.team_run_id` / `sender_agent_id` / `recipient_agent_id` / `message_type` / `content` | 混合 | 是 | 无 | 邮箱路由和消息正文 | 路由标识非空 | 进程内邮箱明文保存 |
| `AgentMessage.correlation_id` / `metadata` / `message_id` / `created_at` | 混合 | 否 | `None` / `{}` / UUID / 当前 UTC | 关联、扩展、去重和排序 | correlation 非空值不得为空；时间带时区 | message_id 永久用于去重 |
| `TeamOrchestratorComponents.planner` / `agents` / `selection_policy` / `verifier` | 协议对象 | 是 | 无 | 规划、目录、调度和任务验收 | 必须满足结构协议 | 生命周期由组合根管理 |
| `TeamOrchestratorComponents.approval_gate` / `repository` / `events` / `aggregator` | 协议对象 | 是 | 无 | 审批、CAS、审计和聚合 | Repository 生产实现必须支持 lease | 事件与仓储可能保存完整输出 |
| `TeamOrchestratorComponents.reviewer` | `TeamReviewer \| None` | 否 | `None` | 团队整体目标验收 | 空值保持兼容接受行为 | 企业生产应显式注入 |

## 核心 DTO 与默认值

### 团队边界

| `TeamLimits` 字段 | 默认值 | 含义 |
| --- | ---: | --- |
| `max_tasks` | `50` | 单次规划最多任务数 |
| `max_concurrency` | `4` | 团队级并行任务上限 |
| `max_task_attempts` | `3` | 每任务最多执行次数 |
| `max_cycles` | `3` | 规划—执行—审查外层循环上限 |
| `max_plan_revisions` | `2` | 人工或审查触发重规划的上限 |
| `timeout_seconds` | `None` | 活跃时间上限；默认不超时 |

`TeamRequest.goal` 必填；`acceptance_criteria=()`、`limits=TeamLimits()`、`metadata={}`。metadata
会复制并递归冻结，但仍应只保存可持久化、非敏感的小对象。

### Agent 与任务

| 类型 | 必填字段 | 主要默认字段 |
| --- | --- | --- |
| `AgentSpec` | `agent_id`、非空 `capabilities` | `max_concurrency=1`、`version="0.1.0"`、`role="worker"`、`description=""`、`metadata={}` |
| `TaskSpec` | `task_id`、`description`、`capability` | `dependencies=()`、`acceptance_criteria=()`、`requires_approval=False`、`priority=0`、`metadata={}` |
| `TaskResult` | `task_id`、`agent_id`、`success` | `output=""`、`artifacts=()`、`error=""`、`attempt=1`、`metadata={}` |
| `TaskVerification` | `passed` | `feedback=""`、`score=None`、`evidence=()`、`failed_criteria=()` |
| `TeamReview` | `action` | `feedback=""`、`score=None`、`evidence=()`、`failed_criteria=()`、`interaction=None` |

任务 ID 在单个计划中必须唯一，依赖不能重复、不能指向自身，整个图不能有未知依赖或环。
Planner 只能使用当前 `AgentDirectory` 能力快照中的 capability；未知能力在执行前失败。

`TeamSnapshot` 是仓储完整状态，关键字段包括 `version/status/tasks/cycle/plan_revision`、
`cycle_history`、`pending_interaction`、`pending_review`、`human_interactions`、
`review_approved_cycle` 以及活跃计时字段。`TeamResult` 是对外只读投影，包含通过任务结果、草稿、
停止原因、循环历史和待处理交互。

### 模型团队组件

| 配置 | 必填 | 默认 |
| --- | --- | --- |
| `ModelTeamPlannerConfig` | `model` | `max_tasks=20`、`max_output_tokens=4096` |
| `ModelTaskVerifierConfig` | `model` | `pass_score=80`、`max_output_tokens=2048` |
| `ModelResultAggregatorConfig` | `model` | `max_output_tokens=4096` |
| `ModelTeamReviewerConfig` | `model` | `pass_score=80`、`max_output_tokens=2048` |

模型 Planner 的 `max_tasks` 还会受到 `TeamLimits.max_tasks` 约束。四个组件都在调用期取得模型租约，
不会持有供应商凭据或自行关闭 Registry 中的客户端。

## 扩展协议

| 协议 | 关键方法 | 企业实现责任 |
| --- | --- | --- |
| `TeamPlanner` | `plan(TeamPlanningContext)` | 只生成已注册能力、有限且无环的任务图 |
| `AgentEndpoint` | `spec`、`execute(AgentTaskContext)` | 幂等执行、组件级超时、返回归属一致的结果 |
| `TaskVerifier` | `verify(context, result)` | 独立于执行 Agent 验证任务级条件 |
| `TeamReviewer` | `review(TeamReviewContext)` | 验收整体目标并返回四种结构化动作 |
| `TeamApprovalGate` | `decide(context)` | 对显式审批任务返回 APPROVED/DEFERRED/REJECTED |
| `AgentSelectionPolicy` | `select(task, candidates, active_counts)` | 只能返回候选快照中的 Agent ID |
| `ResultAggregator` | `aggregate(request, results)` | 聚合已验证通过的结果 |
| `TeamRepository` | create/load/save/list/acquire_lease/release_lease | 持久化完整快照、版本 CAS、控制器互斥和崩溃恢复 |
| `TeamEventPublisher` | `publish(event)` | 有序、可审计地持久化事件并定义失败语义 |

## 状态、动作与停止原因

团队状态为：`CREATED`、`PLANNING`、`RUNNING`、`WAITING_APPROVAL`、`PAUSED`、`BLOCKED`、
`COMPLETED`、`FAILED`、`CANCELLED`、`TIMED_OUT`。只有后四个是 `is_terminal=True`；
`BLOCKED` 通常可由业务处理后恢复，但 `HUMAN_REJECTED` 会保持阻塞并拒绝继续执行。

任务状态为：`PENDING`、`READY`、`WAITING_APPROVAL`、`RUNNING`、`VERIFYING`、`SUCCEEDED`、
`FAILED`、`BLOCKED`、`CANCELLED`。后四个是任务终态。

`TeamReviewAction` 包含 `ACCEPT`、`REPLAN`、`REQUEST_HUMAN`、`STOP`。稳定停止原因包括：

- 正常与人工：`COMPLETED`、`APPROVAL_DEFERRED`、`APPROVAL_REJECTED`、`HUMAN_REJECTED`；
- 执行与调度：`TASK_FAILED`、`NO_CAPABLE_AGENT`、`AGENT_CAPACITY`、`DEADLOCK`；
- 控制边界：`CANCELLED`、`TIMED_OUT`、`CYCLE_LIMIT`、`PLAN_REVISION_LIMIT`、
  `BUDGET_EXHAUSTED`；
- 组件与审查：`COMPONENT_ERROR`、`REVIEW_STOPPED`。

## 生命周期事件

`TeamEvent` 包含必填 `event_type/snapshot`，以及 `detail=""`、`metadata={}` 和默认 UTC 时间。
稳定事件按阶段分组如下：

- 团队：`team.started/paused/resumed/completed/blocked/cancelled/timed_out/failed`；
- 规划：`planning.started`、`plan.created`、`plan.replan_requested`；
- 任务：`task.ready/assigned/started/verifying/verified/retrying/completed/failed`；
- 审批：`approval.requested/granted/rejected`；
- 审查：`review.started/completed`；
- 人工：`human.interaction_requested/response_submitted/approved/rejected/revised/input_provided`。

`LocalTeamEventPublisher` 在进程内按订阅顺序串行调用同步或异步 handler，不持久化、不脱敏、
不隔离 handler 异常。生产环境应实现持久化 `TeamEventPublisher`，并明确失败时是阻止团队推进还是
降级记录。

## 显式装配

```python
from matterloop_agents.collaboration import (
    AgentDirectory,
    AgentSpec,
    AsyncTeamRuntime,
    LeastBusyScheduler,
    LocalTeamEventPublisher,
    LoopAgentEndpoint,
    ModelResultAggregator,
    ModelResultAggregatorConfig,
    ModelTaskVerifier,
    ModelTaskVerifierConfig,
    ModelTeamPlanner,
    ModelTeamPlannerConfig,
    ModelTeamReviewer,
    ModelTeamReviewerConfig,
    TeamOrchestrator,
    TeamOrchestratorComponents,
)

directory = AgentDirectory()
directory.register(
    LoopAgentEndpoint(
        AgentSpec("python-worker", frozenset({"python"}), max_concurrency=2),
        async_loop_runtime,
    )
)

components = TeamOrchestratorComponents(
    planner=ModelTeamPlanner(models, ModelTeamPlannerConfig(model="team-planner")),
    agents=directory,
    selection_policy=LeastBusyScheduler(),
    verifier=ModelTaskVerifier(models, ModelTaskVerifierConfig(model="team-verifier")),
    approval_gate=team_approval_gate,
    repository=team_repository,
    events=team_event_publisher,
    aggregator=ModelResultAggregator(
        models,
        ModelResultAggregatorConfig(model="team-aggregator"),
    ),
    reviewer=ModelTeamReviewer(
        models,
        ModelTeamReviewerConfig(model="team-reviewer"),
    ),
)
runtime = AsyncTeamRuntime(
    TeamOrchestrator(components, owner_id="controller-a"),
    resources=(async_loop_runtime,),
)
```

`reviewer=None` 的真实行为是自动接受所有已通过任务验证的草稿；`ResultSuccessVerifier` 也只检查
`TaskResult.success`。两者适合测试和声明式流程，不是生产语义验收。`AlwaysApproveTeamGate` 同样
只应在明确无需人工控制的流程使用。

## DAG 执行、恢复与热替换

TeamLoop 同时受团队 `max_concurrency` 与每个 `AgentSpec.max_concurrency` 限制。
`LeastBusyScheduler` 先匹配 capability，再按活跃租约数和稳定 Agent ID 选择。Agent 输出会先以
`VERIFYING` 状态持久化，再调用 Verifier；验证阶段崩溃后恢复会复用已保存结果，不重复执行可能
有副作用的 Agent。任务验证失败先重试，尝试耗尽后归档当前 cycle 并重新规划。

`AgentDirectory.replace()` 原子切换后续租约，已有租约继续使用旧端点，活跃容量计数不会因替换
重置。目录不拥有端点生命周期，也不启动或关闭旧端点；调用方必须等旧租约排空后自行释放资源。

`LoopAgentEndpoint` 把任务映射成子 `LoopRequest`，默认使用 Core `LoopLimits()`，并把团队、任务、
Agent usage scope、依赖输出和人工反馈写入 metadata。只有子 Loop `COMPLETED` 才算任务成功；子
Loop 自己产生的 pending interaction 当前不会自动提升为团队交互，只会记录在 TaskResult metadata
并进入失败/重试路径。需要子 Loop 级 HITL 时，应提供能够把交互显式桥接到团队层的自定义端点。

## 人工反馈闭环

```python
from matterloop_core import HumanAction, HumanResponse

paused = await runtime.run(request)
interaction = paused.pending_interaction
if interaction is not None:
    await runtime.submit_human_response(
        paused.run_id,
        HumanResponse(
            interaction_id=interaction.interaction_id,
            action=HumanAction.REVISE,
            content="拆成两个并行证据任务",
            idempotency_key="revision-2026-07-14-01",
        ),
    )
    result = await runtime.resume(paused.run_id)
```

`submit_human_response()` 只原子保存反馈，不自动恢复：

- `APPROVE`：批准当前任务或团队草稿，清除 pending interaction；随后 `resume()` 精确继续，已完成
  Agent 不重放；
- `REJECT`：取消其余任务并进入 `BLOCKED/HUMAN_REJECTED`，后续 resume 返回原阻塞结果；
- `REVISE`：要求非空 content，归档当前 cycle，清空任务和草稿，plan revision 加一；
- `PROVIDE_INPUT`：保存输入并采用与修订相同的重规划路径。

相同幂等键与相同业务内容是 no-op；同键不同内容抛 `HumanResponseConflictError`；interaction ID
不匹配抛 `HumanInteractionNotPendingError`。cycle、revision、模型额度和费用不会因暂停恢复重置。
`PAUSED/BLOCKED/WAITING_APPROVAL` 时间不计入团队 active timeout。

## CAS、控制器租约与生产一致性

每次保存都携带最后观察到的 snapshot version，仓储成功后版本必须加一。并发反馈、取消和恢复
由 CAS 防止覆盖；CAS 冲突不会被静默合并，除非它是完全相同的幂等人工响应。

Orchestrator 运行前同时取得实例内 active 标记和 `TeamRepository` 运行级租约，结束时释放。
`InMemoryTeamRepository` 只适合单进程测试：租约无过期时间，数据也不持久化。当前
`TeamRepository` 协议只有 acquire/release，没有 heartbeat 或 renew；持久化实现必须选择覆盖
最坏执行时长的租约、提供外部 fencing/看门狗，或扩展协议实现续租。租约在活跃运行中提前过期
可能造成重复 Agent 副作用，CAS 只能阻止最终状态覆盖，不能撤销外部操作。

## 资源、安全与生产建议

`AsyncTeamRuntime` 只关闭构造时显式传入 `resources` 的对象，并按逆序调用 `aclose()`；
Orchestrator、Directory、模型、仓储和事件发布器不会自动加入资源列表。`LocalTeamRuntime` 使用
专用事件循环线程，必须 `close()`，且不能从该线程内发起阻塞调用。

- 取消在并行批次安全边界协作完成；自定义组件必须定期进入 `await` 并设置网络/工具超时；
- 组件抛 `ResourceLimitExceededError` 会映射为 `BLOCKED/BUDGET_EXHAUSTED`，不会当普通任务错误
  盲目重试；
- 运行中的一般组件异常会保存为 `FAILED/COMPONENT_ERROR` 结果；重复 run ID、运行不存在、控制器
  租约冲突、非法恢复状态、无法收敛的 CAS 冲突和人工响应冲突仍会抛出类型化异常；外部取消会
  尽力保存 `CANCELLED` 快照后重新抛出 `CancelledError`；
- 团队事件和快照可能包含 goal、输出、人工反馈与 metadata，持久化前必须脱敏并实施租户隔离；
- 外部工具与 Agent endpoint 必须幂等，副作用使用 team run/task/attempt 作为幂等键；
- 上线前必须显式配置总体 reviewer、领域 verifier、审批门、持久化仓储、审计 Publisher、额度
  策略、超时、故障恢复和资源关闭顺序。
