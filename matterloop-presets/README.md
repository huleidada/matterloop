# matterloop-presets

> 跨模块生产拓扑、资源所有权与上线检查见[企业集成指南](../docs/enterprise-integration.md)。

`matterloop-presets` 是 MatterLoop 的组合根：它把模型 Agent、工具注册表、策略、检查点和运行时
装配成四套可直接导入的运行环境，但不新增编排业务逻辑。发行包不读取环境变量、`.env` 或
YAML，也不创建模型 SDK、队列、仓储和审计基础设施。

预设只接收 `matterloop_models.ModelClient`。OpenAI、DeepSeek、MiniMax、千问和智谱等适配器可由
调用方从 `matterloop_models.providers` 构造后注入；模型名、端点和凭据均由应用决定。

## 预设选择

| 预设 | 必填构建参数 | 工具与审批 | 检查点/事件 | 适用边界 |
| --- | --- | --- | --- | --- |
| `minimal` | `model` | 无工具；审批门全放行 | 内存检查点、本地事件 | 开发、测试、纯模型任务 |
| `coding` | `model`、现存目录 `workspace` | 默认执行器只读；高权限执行器可写文件和运行白名单命令；默认延期审批 | 内存检查点、本地事件 | 单进程受控编码 |
| `research` | `model`、现存目录 `workspace`、`config` | 只读文件、HTTPS GET 白名单；审批门全放行 | 内存检查点、本地事件 | 带引用门槛的资料研究 |
| `production` | `model` 及四项显式基础设施 | 默认无工具；审批门默认全放行 | 外部检查点、强失败审计 | 队列 API 与 worker 分离部署 |

前三类使用 `InMemoryCheckpointStore`，进程退出即丢失，不能作为生产恢复方案。production 不提供
内存回退，也不会隐式启动队列消费者。

四组异步/同步 builder 分别是：

| 预设 | 异步 builder | 同步 builder |
| --- | --- | --- |
| minimal | `build_minimal_runtime` | `build_minimal_local_runtime` |
| coding | `build_coding_runtime` | `build_coding_local_runtime` |
| research | `build_research_runtime` | `build_research_local_runtime` |
| production | `build_production_runtime` | `build_production_local_runtime` |

包级还稳定导出五类配置、`PresetRuntime`、`ProductionRuntime`、`ProductionLocalRuntime`，以及
`PresetError/PresetConfigurationError`。

## 公共配置

所有配置都是 `frozen dataclass`。下面是源码中的真实默认值：

| 配置类型 | 适用范围 | 新增字段 |
| --- | --- | --- |
| `AgentPresetConfig` | 全部预设共享基类 | `model_name`、计划、工具、验证、停止和 `retry` |
| `MinimalPresetConfig` | minimal | 无，继承全部共享字段 |
| `CodingPresetConfig` | coding | 高权限执行器、命令、环境、文件和 Shell 限制 |
| `ResearchPresetConfig` | research | host allowlist、文件/HTTP 限制和引用门槛 |
| `ProductionPresetConfig` | production | 无，基础设施通过 builder 参数显式注入 |

| `AgentPresetConfig` 字段 | 默认值 | 含义 |
| --- | ---: | --- |
| `model_name` | `"default"` | 注册到 `ModelRegistry` 的稳定名称 |
| `max_plan_steps` | `20` | 单轮 Planner 最多输出步骤数 |
| `max_tool_rounds` | `8` | Worker 单步骤最多工具反馈轮次 |
| `pass_score` | `80` | Verifier 通过阈值，范围 0–100 |
| `max_identical_feedback` | `2` | 相同失败反馈触发停止前的计数边界 |
| `retry.max_attempts` | `3` | 组件异常最多尝试次数 |
| `retry.base_delay_seconds` | `0.5` | 指数退避基准秒数 |
| `retry.max_delay_seconds` | `30` | 单次退避上限 |
| `retry.jitter_ratio` | `0.2` | 正负随机抖动比例 |

`max_plan_steps` 与 `max_tool_rounds` 不是整个运行的预算。cycle、Executor 总尝试数和活跃超时来自
每个 `LoopRequest.limits`；其默认值分别为 5、20、20 步和无超时。预设不会自动装配 Token、费用
或模型并发额度，企业应用应显式使用 policies 的预算代理和 `UsageLedger`。

### Coding 配置

| 字段 | 默认值 | 约束 |
| --- | ---: | --- |
| `privileged_executor` | `"coding"` | 非空且不能为 `default` |
| `allowed_commands` | `{"pytest", "ruff"}` | 非空；执行时 argv[0] 还必须是裸程序名 |
| `shell_environment` | `{}` | 显式基础环境，不继承宿主环境，且不显示在 repr |
| `max_read_bytes` | `1_000_000` | 单文件读取上限 |
| `max_write_bytes` | `1_000_000` | 单文件写入上限 |
| `max_shell_timeout_seconds` | `60` | 单命令硬上限 |
| `max_shell_output_bytes` | `1_000_000` | stdout 与 stderr 共享上限 |

### Research 配置

| 字段 | 默认值 | 约束 |
| --- | ---: | --- |
| `allowed_hosts` | 字面值为空集合 | 构造时要求至少一个非空 host，因此实际必填 |
| `max_read_bytes` | `1_000_000` | 单文件读取上限 |
| `max_response_bytes` | `2_000_000` | 单 HTTP 响应读取上限 |
| `max_http_timeout_seconds` | `20` | 单请求硬上限 |
| `require_citation` | `True` | 开启本地引用证据门槛 |

`MinimalPresetConfig` 和 `ProductionPresetConfig` 当前只继承共享字段。

## 装配顺序

每个 builder 都按同一顺序创建组件：

1. 以 `config.model_name` 注册调用方模型；
2. 创建 `ModelPlanner`；coding 再包装强制审批规划器；
3. 为每个执行器创建独立 `ToolRegistry` 和 `ToolCallingWorker`；
4. 创建 `CriteriaVerifier`；research 可再包装引用门槛；
5. 装配无进展停止策略、指数退避策略和 `AgentLoop`；
6. 把模型与工具注册表登记为运行时资源，返回 `PresetRuntime`；
7. production 额外创建 `QueueRuntime`，与 worker 运行时组合为 `ProductionRuntime`。

规划、执行和验证默认共享同一模型实例。需要角色隔离、不同限额或不同供应商时，应直接装配基础
组件，而不是把 preset 当成完整生产拓扑。

## Minimal

```python
from matterloop_core import LoopRequest
from matterloop_presets import build_minimal_runtime

async with build_minimal_runtime(model_client) as runtime:
    result = await runtime.run(LoopRequest(goal="整理需求"))
```

工具注册表为空，Worker 无法执行文件、命令或网络操作。检查点和事件都仅在当前进程存在。

## Coding

```python
from matterloop_presets import CodingPresetConfig, build_coding_runtime

runtime = build_coding_runtime(
    model_client,
    workspace="/srv/workspaces/job-42",
    config=CodingPresetConfig(
        allowed_commands=frozenset({"pytest", "ruff"}),
        shell_environment={"PATH": "/opt/matterloop/bin:/usr/bin"},
    ),
    approval_gate=approval_gate,
)
```

`default` 执行器只有只读 `filesystem`；`coding`（或自定义高权限名称）同时拥有可写文件工具和
受限 Shell。无论模型是否设置 `requires_approval`，指向高权限执行器的步骤都会被提升为需要
审批。未传 `approval_gate` 时，默认决策是 `DEFERRED`，Loop 产生待处理人工交互并暂停。

Shell 只接收 argv，不经过 shell 解释器；子进程默认环境为空。此预设仍不是恶意代码隔离边界，
不可信代码应运行在独立容器、虚拟机或远程沙箱中。文件工具的根目录约束也不能抵御同机恶意进程
的并发路径替换。

## Research

```python
from matterloop_presets import ResearchPresetConfig, build_research_runtime

runtime = build_research_runtime(
    model_client,
    workspace="/srv/reference-data",
    config=ResearchPresetConfig(
        allowed_hosts=frozenset({"docs.example.com"}),
    ),
)
```

HTTP 固定为 HTTPS `GET`、精确 host allowlist 且不跟随重定向，文件工具固定只读。引用门槛只检查
验证证据是否包含 `http://`、`https://`、`artifact://`，或执行制品 URI 是否以这些前缀开头；
它不下载、签名或证明来源可信。高保证研究流程应接入领域验证器和独立证据存储。

## Production

```python
from matterloop_presets import build_production_runtime

runtime = build_production_runtime(
    model_client,
    queue_backend=queue_backend,
    run_repository=run_repository,
    checkpoint_store=checkpoint_store,
    audit_publisher=audit_publisher,
    event_reader=event_reader,
    approval_gate=approval_gate,
)
```

四项依赖虽然类型签名允许 `None`，但在运行时均为必填，缺少时立即抛
`PresetConfigurationError`：

| 参数 | 必须提供的语义 |
| --- | --- |
| `queue_backend` | `QueueBackend`：enqueue、lease、acknowledge、release、cancel |
| `run_repository` | `RunRepository`：创建、查询、分页及版本 CAS |
| `checkpoint_store` | Core `CheckpointStore`：精确恢复和 revision CAS |
| `audit_publisher` | Core `EventPublisher`：被包装为 `PublisherFailureMode.RAISE` |

`event_reader` 可选；未传且 audit publisher 同时实现 `RunEventReader` 时自动复用，否则
`list_events()` 返回空元组。`approval_gate` 未传时使用 `AllowAllApproval`；当前 production 默认
无工具，但一旦扩展高权限执行器，应同时注入真实审批策略。

`ProductionRuntime` 的 API/调度入口提供 `submit/get/list/result/wait/cancel/resume/list_events`；
实际 worker 使用 `worker_runtime.run()` 或 `worker_runtime.resume()`。预设不租用消息、不更新
RunRecord、不续租，也不调用 acknowledge/release，这些都是部署方 worker 循环的职责。必须通过
幂等执行、仓储 CAS 和队列租约共同处理至少一次投递。

队列门面的参数默认值为：`submit(run_id=None)`、`list(limit=100, offset=0)`、
`wait(timeout_seconds=None, poll_interval_seconds=0.1)`、`resume(mode=CONTINUE)`、
`list_events(after=None, limit=100)`。`ProductionLocalRuntime` 只把 worker 变成同步接口；其
`queue_runtime` 属性仍是异步队列客户端。

## 运行时、人工反馈与资源生命周期

`PresetRuntime` 公开 `loop`、`models`、`tool_registries`、`checkpoint_store` 和 `config`，可用于
检查装配结果或对新事务执行热替换。它继承异步 `run/resume/submit_human_response/cancel` 门面；
提交人工反馈不会自动恢复，调用方需随后显式 `resume()`。

异步运行时使用 `async with` 或 `aclose()`；同步 builder 返回使用专用事件循环线程的
`LocalRuntime`/`ProductionLocalRuntime`，使用 `with` 或 `close()`。关闭时会逆序关闭登记资源：
工具注册表以及任何实现 `aclose()` 的原始模型适配器。适配器是否继续关闭底层 SDK 客户端取决于
其所有权配置。热替换后新增模型或工具的生命周期不会自动加入初始资源列表，调用方必须管理退役
实例和替换实例的关闭。

`ProductionRuntime.aclose()` 只关闭 worker runtime；队列、仓储、Redis 客户端和审计后端均为
调用方所有，应在停止投递、排空 worker 并关闭 runtime 后再释放。

## 失败与生产检查

- 配置值非法通常抛 `ValueError`；安全依赖缺失或执行器映射不完整抛
  `PresetConfigurationError`；工具构造还可能抛 `ToolConfigurationError`。
- Coding 默认延期审批表现为 `PAUSED`，不是组件故障；Research 缺少引用会被降级为验证失败并
  继续受 cycle/stop 策略约束。
- Production 审计失败会传播并阻止继续；事件发布器必须可用、幂等且满足保留要求。
- 模型、网络和工具组件应各自设置超时；同步门面不应从它自己的事件循环线程内阻塞调用。
- 上线前应显式设置 `LoopRequest.limits.timeout_seconds`、预算账本、租户 metadata、持久化检查点、
  审计脱敏、队列死信策略以及优雅关闭顺序。
