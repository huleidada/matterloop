简体中文 | [English](https://github.com/huleidada/matterloop/blob/main/matterloop-agents/README.en.md)

# matterloop-agents

这个包提供两层能力：可直接注入 Core 的标准 Agent 组件，以及由中心控制器管理的 TeamLoop。两层
共用模型与工具协议，但状态边界不同；TeamLoop 不靠 Agent 之间自由聊天来维持一致性。

```bash
pip install matterloop-agents
```

## 单 Agent：Planner、Worker、Verifier

```python
from matterloop_agents import (
    CriteriaVerifier,
    CriteriaVerifierConfig,
    ModelPlanner,
    ModelPlannerConfig,
    ToolCallingWorker,
    ToolCallingWorkerConfig,
)

planner = ModelPlanner(models, ModelPlannerConfig(model="planner"), memory=memory)
worker = ToolCallingWorker(
    models,
    tools,
    ToolCallingWorkerConfig(model="worker", tool_names=("filesystem",)),
)
verifier = CriteriaVerifier(models, CriteriaVerifierConfig(model="verifier"))
```

`ModelPlanner` 生成有限步骤计划，`ToolCallingWorker` 执行工具续轮，`CriteriaVerifier` 独立判断步骤
是否达到验收条件。`ModelReviewer` 可做更开放的质量审查，并通过 adapter 转成 Core Verifier。

每次模型事务都通过 `ModelRegistry.acquire()` 固定客户端；热替换只影响新事务。模型 JSON、计划
步骤和工具参数仍会在本地严格解析。计划超限、非法工具、解析失败和工具循环超限使用类型化 Agent
异常，由 Core 的重试策略决定是否重试。

<details>
<summary>单 Agent 配置速查</summary>

- `ModelPlannerConfig(model, default_executor, max_steps, max_output_tokens, memory_namespace, memory_limit)`：默认执行器 `default`、20 步、4096 输出 Token、记忆 namespace `default`、最多 5 条记忆。
- `ToolCallingWorkerConfig(model, tool_names, max_tool_rounds, max_output_tokens)`：默认无工具、最多 8 轮、每轮 4096 输出 Token。
- `CriteriaVerifierConfig(model, pass_score, max_output_tokens)`：默认 80 分通过、2048 输出 Token。
- `ModelReviewerConfig(model, max_output_tokens)`：默认 3072 输出 Token。
- `ReviewResult(score, summary, evidence, issues, recommendations)`：通用审查结果；issues 非空时 adapter 不会判定通过。

这些上限约束单次组件调用，不是整个运行的 Token、费用或 cycle 预算。

</details>

## TeamLoop：DAG，而不是群聊

```text
TeamPlanner
  └─ 根据能力快照、人工反馈和历史审查生成 TaskSpec DAG
       └─ TeamOrchestrator 找出 READY 任务
            ├─ 审批 requires_approval 的任务
            ├─ AgentDirectory 分配有容量的 Endpoint
            └─ 并行执行 → TaskVerifier
                         └─ fan-in → ResultAggregator → TeamReviewer
                                      ├─ ACCEPT
                                      ├─ REPLAN
                                      ├─ REQUEST_HUMAN
                                      └─ STOP
```

`TeamOrchestrator` 是 `TeamSnapshot` 的唯一写入者。Endpoint 只收到隔离的 `AgentTaskContext` 并返回
`TaskResult`；批次结果在 fan-in 后由控制器顺序提交。依赖任务的正式通信通道是
`dependency_results`。Mailbox 和 ArtifactStore 是可选设施，不能绕过控制器修改全局状态。

Planner 只能选择 `AgentDirectory` 当前能力快照中的 capability。任务图必须无环、依赖存在、ID
唯一，并同时受到团队并发和每个 Agent 容量限制。

## 最小团队装配

```python
from matterloop_agents.collaboration import (
    AgentDirectory,
    AgentSpec,
    AsyncTeamRuntime,
    LeastBusyScheduler,
    LoopAgentEndpoint,
    TeamOrchestrator,
    TeamOrchestratorComponents,
)

directory = AgentDirectory()
directory.register(
    LoopAgentEndpoint(
        AgentSpec("python-worker", frozenset({"python"}), max_concurrency=2),
        child_runtime,
    )
)

components = TeamOrchestratorComponents(
    planner=team_planner,
    agents=directory,
    selection_policy=LeastBusyScheduler(),
    verifier=task_verifier,
    approval_gate=approval_gate,
    repository=team_repository,
    events=team_events,
    aggregator=result_aggregator,
    reviewer=team_reviewer,
)
runtime = AsyncTeamRuntime(
    TeamOrchestrator(components, owner_id="controller-a"),
    resources=(child_runtime,),
)
```

`reviewer=None` 会自动接受所有已通过任务验证的草稿；`ResultSuccessVerifier` 也只检查 success 标志。
它们适合测试，不构成生产验收。生产团队应配置领域 Verifier、整体 Reviewer 和真实审批门。

模型版团队组件使用 `ModelTeamPlannerConfig(model, max_tasks, max_output_tokens)`、
`ModelTaskVerifierConfig(model, pass_score, max_output_tokens)`、
`ModelResultAggregatorConfig(model, max_output_tokens)` 与
`ModelTeamReviewerConfig(model, pass_score, max_output_tokens)`。它们只保存注册名，不持有凭据。

## 重试、重新规划与恢复点

任务结果先保存为 `VERIFYING`，再调用 Verifier。验证阶段崩溃后恢复会复用已保存结果，不重复执行
可能有副作用的 Endpoint。验证失败先消耗任务级 attempt；耗尽后才归档当前 cycle，带着失败审查
进入下一轮规划。

`TeamLimits` 分开限制单计划任务数、并发任务、单任务尝试、团队 cycle、计划 revision 和活跃超时。
暂停不会重置任何计数或用量，也不计入 active timeout。

`AgentDirectory.replace()` 只影响新租约；已有任务继续使用旧 Endpoint。Directory 不拥有 Endpoint
生命周期，应用要等待旧租约排空后关闭旧资源。

`LoopAgentEndpoint` 把团队任务映射为子 `LoopRequest`，并传入依赖结果、人工反馈和父子 usage scope。
当前子 Loop 自己产生的 pending interaction 不会自动提升到团队层；需要这种行为时，应实现专用
Endpoint 桥接两级 HITL。

## 人工反馈

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
            idempotency_key="revision-01",
        ),
    )
    result = await runtime.resume(paused.run_id)
```

提交反馈只写入仓储，不隐式恢复。`APPROVE` 精确继续且不重放已完成任务；`REJECT` 进入
`BLOCKED/HUMAN_REJECTED`；`REVISE` 和 `PROVIDE_INPUT` 保存历史并重新规划。相同幂等键与相同内容
是 no-op，同键不同内容抛冲突异常。

## 持久化与控制器租约

每次 Snapshot 保存都使用 version CAS。运行前，Orchestrator 还会取得运行级控制器租约，防止两个
控制器同时推进同一个团队。`InMemoryTeamRepository` 没有持久化或租约过期，只适合测试。

当前 `TeamRepository` 只有 acquire/release，没有 heartbeat/renew。生产实现应使用覆盖最坏执行
时间的租约、外部 fencing 或扩展续租协议。租约过早失效可能造成 Endpoint 副作用重复；CAS 只能
阻止最终状态覆盖，不能撤销已经发生的外部操作。

<details>
<summary>TeamLoop 公共数据结构速查</summary>

- `TeamRequest(goal, acceptance_criteria, limits, metadata)`。
- `TeamLimits(max_tasks, max_concurrency, max_task_attempts, max_cycles, max_plan_revisions, timeout_seconds)`：默认 50、4、3、3、2 和无超时。
- `AgentSpec(agent_id, capabilities, max_concurrency, version, description, role, metadata)`。
- `TaskSpec(task_id, description, capability, dependencies, acceptance_criteria, requires_approval, priority, metadata)`。
- `AgentTaskContext(team_run_id, request, task, agent_id, attempt, dependency_results, previous_error, human_feedback)`。
- `TaskResult(task_id, agent_id, success, output, artifacts, error, attempt, metadata)`。
- `TaskVerification(passed, feedback, score, evidence, failed_criteria)`。
- `TaskState(spec, status, attempt, approval_granted, assigned_agent, result, verification, error)`。
- `TeamPlanningContext(run_id, request, cycle, plan_revision, available_agents, prior_reviews, human_feedback)`。
- `TeamReviewContext(run_id, request, cycle, plan_revision, task_results, draft_output, prior_reviews, human_feedback)`。
- `TeamReview(action, feedback, score, evidence, failed_criteria, interaction)`。
- `TeamCycleRecord(cycle, plan_revision, tasks, draft_output, review, error)`。
- `TeamSnapshot(request, tasks, run_id, status, version, stop_reason, output, error, cycle, plan_revision, cycle_history, pending_interaction, pending_review, human_interactions, review_approved_cycle, active_elapsed_seconds, active_started_at, created_at, updated_at)`。
- `TeamResult(run_id, status, task_results, output, stop_reason, error, cycle, cycle_history, pending_interaction, human_interactions, started_at, finished_at)`。
- `TeamEvent(event_type, snapshot, detail, metadata, occurred_at)`：事件携带当时的完整快照，可能很大且敏感。
- `AgentMessage(team_run_id, sender_agent_id, recipient_agent_id, message_type, content, correlation_id, metadata, message_id, created_at)`：可选 Mailbox DTO，不是全局状态通道。
- `TeamOrchestratorComponents(planner, agents, selection_policy, verifier, approval_gate, repository, events, aggregator, reviewer)`。

`TeamReviewAction` 为 `ACCEPT/REPLAN/REQUEST_HUMAN/STOP`。团队停止原因区分完成、审批/人工拒绝、任务
失败、无可用 Agent、容量、死锁、取消、超时、cycle/revision 上限、预算耗尽和组件错误。

</details>

## 生产边界

- Endpoint、工具和业务写操作必须使用 team run/task/attempt 作为幂等键。
- `ResourceLimitExceededError` 映射为 `BLOCKED/BUDGET_EXHAUSTED`，不作为普通任务错误重试。
- Team 事件可能包含 goal、完整输出、人工意见和 metadata；Publisher 与 Repository 都要实施租户
  隔离、加密、保留期和脱敏。
- `AsyncTeamRuntime` 只关闭 `resources` 中的对象；Directory、模型、仓储和事件后端不会被自动接管。
- `LocalTeamRuntime` 使用专用事件循环线程，必须关闭，也不能在该线程内阻塞调用自己。

完整状态与模块边界见[架构说明](../docs/architecture.md)，跨进程部署见
[企业集成指南](../docs/enterprise-integration.md)。
