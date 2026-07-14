# matterloop-runtime

> 跨模块生产拓扑、资源所有权与上线检查见[企业集成指南](../docs/enterprise-integration.md)。

MatterLoop 的运行门面、队列抽象、本地进程沙箱和组件生命周期容器。

```bash
pip install matterloop-runtime
```

本包不会提供恶意代码隔离。`LocalProcessSandbox` 仅限制工作目录、环境、运行时间与输出量；
处理不可信代码时，应替换为容器、虚拟机或远程沙箱实现。

`LocalProcessSandbox` 默认使用空的子进程环境，不继承宿主进程的 `PATH` 或其他变量。需要按
程序名查找可执行文件时，必须由应用显式传入基础环境：

```python
from matterloop_runtime import LocalProcessSandbox

sandbox = LocalProcessSandbox(
    workspace,
    base_environment={"PATH": "/opt/matterloop/bin:/usr/bin"},
)
```

所有路径都按调用方给出的字面值解析；组件不会通过 `HOME` 展开 `~`。应用应在构造组件前自行
解析路径和加载配置。

```python
from matterloop_core import HumanAction, HumanResponse
from matterloop_runtime import AsyncRuntime, LocalRuntime

async_runtime = AsyncRuntime(agent_loop)
with LocalRuntime(async_runtime) as runtime:
    result = runtime.run(request)
    if result.pending_interaction is not None:
        runtime.submit_human_response(
            result.run_id,
            HumanResponse(
                result.pending_interaction.interaction_id,
                HumanAction.APPROVE,
            ),
        )
        result = runtime.resume(result.run_id)
```

提交人工响应只更新检查点；异步和同步门面都要求调用方随后显式 `resume()`。

## 稳定公共入口

包级 `matterloop_runtime.__all__` 导出以下 API：

| 分组 | 公共 API |
|---|---|
| 运行门面 | `LoopEngine`、`AsyncClosable`、`AsyncRuntime`、`LocalRuntime` |
| 队列 DTO 与枚举 | `QueueAction`、`RunStatus`、`QueuedRun`、`QueueLease`、`RunRecord` |
| 队列协议与门面 | `QueueProducer`、`QueueBackend`、`RunRepository`、`RunEventReader`、`QueueRuntime` |
| 开发实现 | `InMemoryQueueBackend`、`InMemoryRunRepository` |
| 生命周期容器 | `RuntimeContainer` |
| 沙箱 | `Sandbox`、`ProcessRequest`、`ProcessResult`、`LocalProcessSandbox` |
| 异常 | `RuntimeErrorBase`、`RuntimeClosedError`、`ComponentExistsError`、`ComponentNotFoundError`、`DuplicateRunError`、`RunNotFoundError`、`RunNotResumableError`、`SandboxError`、`SandboxPathError` |

## 企业装配模式

### 直接异步运行

```python
from matterloop_runtime import AsyncRuntime

runtime = AsyncRuntime(
    agent_loop,
    resources=(closable_model_client, closable_tool_client, closable_database_client),
)

async with runtime:
    result = await runtime.run(request)
```

`resources` 必须实现 `aclose()`，并按注册逆序关闭。`AgentLoop`、CheckpointStore 或其他需要关闭
的对象只有显式放入 resources 才由门面管理。

### 队列控制面

```python
from matterloop_runtime import QueueRuntime

queue_runtime = QueueRuntime(
    producer=queue_producer,
    repository=shared_run_repository,
    event_reader=optional_event_reader,
)

run_id = await queue_runtime.submit(request)
record = await queue_runtime.wait(run_id, timeout_seconds=30)
```

`QueueRuntime` 是提交、查询、恢复和取消的控制面，不包含 Worker 执行循环。生产系统还必须部署
独立 Worker：消费 QueueBackend/Celery 命令、调用 AsyncRuntime、用 RunRepository CAS 保存结果，
最后确认或释放消息。

### 同步门面

`LocalRuntime` 在构造时启动一个 daemon 事件循环线程。推荐始终使用 `with`，确保关闭异步资源
并回收线程；不要为每次业务调用创建一个 LocalRuntime。

## 运行门面构造器与方法

### 构造器

| 类型 | 参数 | 必填 | 默认 | 业务含义 | 生命周期约束 |
|---|---|---:|---|---|---|
| `AsyncRuntime` | `engine: LoopEngine` | 是 | 无 | 实际执行 Loop 的内核 | 门面不自动启动 engine |
| `AsyncRuntime` | `resources: Iterable[AsyncClosable]` | 否 | `()` | 统一托管的异步资源 | `aclose` 逆序尝试全部关闭，并在结束后抛第一个错误 |
| `LocalRuntime` | `runtime: AsyncRuntime` | 是 | 无 | 被包装的异步门面 | `close` 会调用其 `aclose` |
| `LocalRuntime` | `thread_name: str` | 否 | `"matterloop-runtime"` | 后台线程名称 | 线程为 daemon；构造器等待事件循环就绪 |
| `QueueRuntime` | `producer: QueueProducer` | 是 | 无 | 推送或入队命令 | 不取得 producer 关闭责任 |
| `QueueRuntime` | `repository: RunRepository` | 是 | 无 | 运行查询和 CAS 状态 | API、Worker 必须共享同一逻辑仓储 |
| `QueueRuntime` | `event_reader: RunEventReader \| None` | 否 | `None` | 审计事件分页 | 为空时 `list_events` 返回空元组，不报能力错误 |
| `RuntimeContainer` | `components: Mapping[str, T] \| None` | 否 | `None` | 初始组件快照 | 构造时不会调用这些组件的 `start` |
| `LocalProcessSandbox` | `root: str \| Path` | 是 | 无 | 允许 cwd 的根路径 | 按字面值 `resolve()`，不展开 `~` |
| `LocalProcessSandbox` | `base_environment: Mapping[str, str] \| None` | 否 | `None` | 子进程基础环境 | `None` 表示完全空环境，不继承宿主变量 |

### `LoopEngine` 与门面方法

| 方法 | 参数与默认 | 返回值 | 语义 |
|---|---|---|---|
| `create_run_id()` | 无 | `str` | 在启动前取得 run id，便于并发取消 |
| `run(request, run_id=None)` | request 必填 | `LoopResult` | 启动新 Loop |
| `resume(run_id, mode=CONTINUE)` | run id 必填 | `LoopResult` | 精确继续或重新规划 |
| `submit_human_response(run_id, response)` | 两者必填 | `LoopResult` | 只提交反馈，不隐式 resume |
| `cancel(run_id)` | run id 必填 | `bool` | 请求在安全边界取消；AsyncRuntime 兼容同步或 awaitable engine 返回值 |

AsyncRuntime 关闭后上述方法和 `create_run_id` 抛 `RuntimeClosedError`；`aclose` 可重复调用。
LocalRuntime 的同步方法阻塞等待结果，不能从它自己的事件循环线程调用，否则抛 `RuntimeError`，
避免线程自锁。

## 队列 DTO 字段

### `QueuedRun`

| 字段 | 类型 | 必填 | 默认 | 业务含义 | 校验不变量 | 安全与持久化 |
|---|---|---:|---|---|---|---|
| `run_id` | `str` | 是 | 无 | 跨队列运行标识 | 不得为空 | 应作为幂等键，不应包含凭据 |
| `action` | `QueueAction` | 是 | 无 | `START` 或 `RESUME` | START 必须带 request；RESUME 禁止带 request | 只序列化 DTO，不传 Runtime 实例 |
| `request` | `LoopRequest \| None` | 否 | `None` | START 的完整请求 | 见 action 约束 | 可能含敏感目标和 metadata |
| `resume_mode` | `ResumeMode` | 否 | `CONTINUE` | RESUME 模式 | DTO 不限制 START 携带该默认值 | Worker 必须按枚举解析 |
| `enqueued_at` | `datetime` | 否 | 当前 UTC | 入队时间 | 必须带时区 | 用于审计，不决定租约顺序 |

### `QueueLease`

| 字段 | 类型 | 必填 | 默认 | 业务含义 | 校验不变量 | 安全与持久化 |
|---|---|---:|---|---|---|---|
| `lease_id` | `str` | 是 | 无 | 本次租约唯一标识 | 非空 | ack/release 必须原样提交 |
| `job` | `QueuedRun` | 是 | 无 | 被租用命令 | 继承命令不变量 | 可能含敏感 request |
| `worker_id` | `str` | 是 | 无 | Worker 标识 | 非空 | 不等于认证凭据 |
| `expires_at` | `datetime` | 是 | 无 | 租约到期时间 | 必须带时区 | 无内建续租协议 |
| `attempt` | `int` | 否 | `1` | 消息交付尝试次数 | 至少 1 | 与 Core Executor attempt 无关 |

### `RunRecord`

| 字段 | 类型 | 必填 | 默认 | 业务含义 | 校验不变量 | 安全与持久化 |
|---|---|---:|---|---|---|---|
| `run_id` | `str` | 是 | 无 | 仓储主键 | 非空 | 应建立唯一约束 |
| `request` | `LoopRequest` | 是 | 无 | 原始请求 | 继承 Core 不变量 | 持久化时加密与租户隔离 |
| `status` | `RunStatus` | 否 | `QUEUED` | 队列视角状态 | DTO 不额外检查类型 | CAS 更新 |
| `version` | `int` | 否 | `0` | 仓储 CAS 版本 | 不得为负 | replacement 必须严格加 1 |
| `result` | `LoopResult \| None` | 否 | `None` | 已产生的 Core 结果 | 非空时 run id 必须匹配 | 可能含输出、反馈和错误 |
| `error` | `str` | 否 | `""` | 队列/Worker 错误摘要 | 允许空 | 对外返回前必须脱敏 |
| `created_at` | `datetime` | 否 | 当前 UTC | 创建时间 | 必须带时区 | 分页排序依据 |
| `updated_at` | `datetime` | 否 | 当前 UTC | 最近状态更新时间 | 必须带时区 | 陈旧 Worker 认领可能依赖该值 |

## 队列状态与协议

### 枚举

| 枚举 | 值与语义 |
|---|---|
| `QueueAction` | `START` 创建新运行；`RESUME` 恢复已有运行 |
| `RunStatus` | `QUEUED`、`RUNNING`、`PAUSED`、`BLOCKED`、`COMPLETED`、`FAILED`、`CANCELLED`、`TIMED_OUT` |

`RunStatus.is_settled` 对除 QUEUED/RUNNING 外的状态为真，因此 `wait()` 会在 PAUSED 或 BLOCKED
返回；`is_terminal` 只包含 COMPLETED、FAILED、CANCELLED、TIMED_OUT。

### 扩展协议

| 协议 | 方法 | 业务契约 |
|---|---|---|
| `QueueProducer` | `enqueue(job)`、`cancel(run_id)` | Celery 等推送系统只需实现这两个方法 |
| `QueueBackend` | Producer 方法加 `lease(worker_id, lease_seconds=None)`、`acknowledge(lease)`、`release(lease, delay_seconds=0)` | 主动拉取 Worker 使用；租约后端负责原子所有权 |
| `RunRepository` | `create`、`get`、`list(limit=100, offset=0)`、`compare_and_set(run_id, expected_version, replacement)` | create 拒绝重复；CAS replacement.version 必须加 1 |
| `RunEventReader` | `list_events(run_id, after=None, limit=100)` | 返回只读映射；after 的格式由实现定义 |
| `Sandbox` | `run(ProcessRequest) -> ProcessResult` | 可替换进程执行边界 |
| `AsyncClosable` | `aclose()` | AsyncRuntime 统一资源释放协议 |

## `QueueRuntime` 操作语义

| 方法 | 参数默认 | 返回值 | 并发与失败语义 |
|---|---|---|---|
| `submit(request, run_id=None)` | 自动 UUID hex | `str` | 先 create 记录再 enqueue；入队失败会尽力 CAS 标记 FAILED，然后重抛 |
| `get(run_id)` | 无 | `RunRecord \| None` | 直接读取仓储 |
| `list(limit=100, offset=0)` | 由仓储校验 | `tuple[RunRecord, ...]` | 预期按创建时间倒序 |
| `result(run_id)` | 无 | `LoopResult \| None` | 记录不存在和尚无结果都返回 `None` |
| `wait(run_id, timeout_seconds=None, poll_interval_seconds=0.1)` | 无总超时、100ms 轮询 | `RunRecord` | PAUSED/BLOCKED 也会返回；poll 必须大于 0；超时抛内建 `TimeoutError` |
| `cancel(run_id)` | 无 | `bool` | 不存在或终态为假；PAUSED/BLOCKED 直接 CAS 取消；其他状态先请求 producer |
| `resume(run_id, mode=CONTINUE)` | 精确继续 | `bool` | 仅 PAUSED/BLOCKED；先 CAS 到 QUEUED 再入队，失败会尽力恢复旧状态 |
| `list_events(run_id, after=None, limit=100)` | 无 reader 时为空 | 事件元组 | Runtime 本身不验证 run 是否存在 |

内部状态更新最多尝试 16 次 CAS，每次竞争失败 `await asyncio.sleep(0)` 让出调度。耗尽后返回
`False`，不会无限重试。

## 进程内队列和仓储

### `InMemoryRunRepository`

- 使用 `asyncio.Lock` 提供单进程原子 create/get/list/CAS。
- create 重复 run id 抛 `DuplicateRunError`。
- list 要求 `limit >= 1`、`offset >= 0`，按 `created_at` 倒序。
- CAS 在记录缺失或版本不匹配时返回 `False`；replacement run id 必须一致且 version 严格加 1。

### `InMemoryQueueBackend`

- 同一 run id 在 pending 或 leased 生命周期内只能存在一条命令，重复 enqueue 抛
  `DuplicateRunError`。
- `lease_seconds=None` 的真实默认租约为 `30.0` 秒；worker id 非空，租约必须大于 0。
- 过期租约只在下一次 `lease()` 时回收，attempt 加 1 并重新排队。
- release 支持 `delay_seconds=0`，不得为负；acknowledge 和无效租约操作是安全空操作。
- cancel 会移除尚未租用命令；已租用命令只是标记取消，在 release 或过期回收时清理，不会中断
  正在工作的协程。
- 两个内存实现都不持久化、不跨进程通知、不限制容量，仅适合测试和本地开发。

## `RuntimeContainer` 生命周期与热替换

| 方法 | 参数默认 | 行为与失败边界 |
|---|---|---|
| `register(name, component, replace=False)` | 默认禁止覆盖 | 先调用新组件可选 `start()`；失败时尽力 `aclose()`，旧组件不变 |
| `replace(name, component)` | name 必须已存在 | 新组件启动成功后原子换入；旧组件无活动借用时关闭 |
| `unregister(name)` | 无 | 从新查询中移除；等待已有 acquire 调用退出后关闭 |
| `get(name)` | 无 | 返回当前实例但不固定生命周期，长调用不应使用 |
| `names()` | 无 | 返回排序名称快照 |
| `acquire(name)` | 异步上下文管理器 | 固定本次调用实例；替换只影响新 acquire |
| `aclose()` | 可重复 | 禁止新调用；空闲组件立即关闭，活动组件在最后一个借用退出后关闭 |

组件的 `start` 和 `aclose` 都可以是同步或异步方法。构造器传入的初始 mapping 被视为已启动；
若需要统一启动回滚语义，应创建空容器后逐个 `await register()`。组件关闭应设计为幂等且尽量
不抛异常；替换完成后的旧组件关闭错误仍会向调用方传播。`RuntimeContainer.aclose()` 当前不聚合
关闭异常，第一个空闲组件关闭失败可能中断后续关闭；企业组件应自行吞吐可恢复清理错误，或由
组合根增加关闭编排和告警。

## 进程请求字段

### `ProcessRequest`

| 字段 | 类型 | 必填 | 默认 | 业务含义 | 校验不变量 | 安全边界 |
|---|---|---:|---|---|---|---|
| `argv` | `tuple[str, ...]` | 是 | 无 | 程序和参数 | 至少一项、首项非空、任何项不得含 NUL | 直接传给 `create_subprocess_exec`，不经过 Shell |
| `cwd` | `str \| Path` | 否 | `"."` | 工作目录 | 运行时 resolve 后必须在 root 内且为已存在目录 | 可阻止 `..` 和符号链接逃逸 |
| `environment` | `Mapping[str, str]` | 否 | `{}` | 覆盖/追加环境 | 键值必须为字符串；键非空且不得含 `=`/NUL，值不得含 NUL；repr 隐藏 | 覆盖同名 base environment，不继承宿主环境 |
| `stdin` | `bytes \| None` | 否 | `None` | 标准输入 | 无额外大小限制 | 大输入仍占内存，应由上层限额 |
| `timeout_seconds` | `float` | 否 | `30.0` | 最长等待时间 | 必须大于 0 | 超时 kill 直接子进程 |
| `max_output_bytes` | `int` | 否 | `1_000_000` | stdout/stderr 共享保留预算 | 至少 1 | 继续排空但丢弃超限内容，避免管道阻塞 |

### `ProcessResult`

| 字段 | 类型 | 必填 | 默认 | 业务含义 | 安全边界 |
|---|---|---:|---|---|---|
| `return_code` | `int` | 是 | 无 | 子进程返回码 | 调用方负责解释非零和信号值 |
| `stdout` | `str` | 是 | 无 | UTF-8 输出 | 非法字节使用替换字符；可能含敏感信息 |
| `stderr` | `str` | 是 | 无 | UTF-8 错误输出 | 同上 |
| `duration_seconds` | `float` | 是 | 无 | 单调时钟耗时 | 用于观测，不是计费凭据 |
| `timed_out` | `bool` | 否 | `False` | 是否因 timeout kill | 不表示子进程树全部终止 |
| `truncated` | `bool` | 否 | `False` | 输出是否超过共享预算 | stdout/stderr 谁保留更多取决于异步到达顺序 |

## 沙箱安全边界

- `LocalProcessSandbox` 明确不是恶意代码隔离。它不限制系统调用、网络、CPU、内存、文件数量、
  用户权限或可执行文件来源。
- 不使用 `shell=True`，但 argv 指向的程序本身仍可能解释脚本或执行任意操作。调用方必须做命令
  allowlist、参数策略和审批。
- timeout 或协程取消只 kill 直接子进程，不保证清理其派生进程组。生产实现应使用容器、作业对象
  或远程沙箱处理进程树。
- root/cwd 只限制进程启动目录，不限制进程随后访问沙箱根目录外的文件。
- 输出、stdin 和环境都是内存数据，库不脱敏、不加密、不写审计；日志层不得直接记录完整请求。

## 错误语义

| 异常 | 触发条件 |
|---|---|
| `RuntimeClosedError` | AsyncRuntime、LocalRuntime 或 RuntimeContainer 已关闭后再接收新操作 |
| `ComponentExistsError` | Container 重复注册且未允许 replace |
| `ComponentNotFoundError` | Container get/acquire/replace/unregister 找不到名称 |
| `DuplicateRunError` | QueueBackend 或 RunRepository 已存在 run id |
| `RunNotFoundError` | wait/resume 或内部 CAS 找不到运行 |
| `RunNotResumableError` | Queue 记录不是 PAUSED/BLOCKED |
| `SandboxPathError` | cwd 逃逸 root 或目录不存在 |
| `SandboxError` | 可替换 Sandbox 的统一基类；本地进程创建等原生 OS 错误可能直接传播 |

## 企业并发、持久化与当前限制

- QueueBackend 的租约和 RunRepository 的 CAS 是两种独立并发机制；Worker 必须同时正确处理消息
  重投和状态竞争，不能把“拿到消息”当作全局唯一执行证明。
- 队列、仓储、事件读取器和 Worker 必须使用一致的 run id、序列化版本和租户 namespace。
- `QueueRuntime.cancel()` 是尽力而为，成功只表示请求已登记；运行组件仍需在安全边界响应取消。
- Runtime 不提供通用队列 Worker、租约续期器、心跳、分布式锁、调度器或死信队列。
- Runtime 不读取环境变量，也不创建 Celery、Redis、数据库或模型客户端；凭据和客户端由组合根
  显式注入并决定所有权。
- `LocalRuntime` 适合阻塞式应用，不应放进 FastAPI 等异步请求路径，也不应跨 fork 复用其线程。
- `LocalProcessSandbox` 只用于受信任或额外隔离后的命令；处理不可信代码必须替换实现。
