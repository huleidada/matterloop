# matterloop-integration-celery

> 跨模块生产拓扑、资源所有权与上线检查见[企业集成指南](../docs/enterprise-integration.md)。

`matterloop-integration-celery` 把 MatterLoop 的 `QueueProducer` 适配到 Celery 推送式任务系统，并在 Worker 侧注册带共享仓储 CAS 认领的启动与恢复任务。

本包不把 Runtime、模型、工具、数据库客户端或其他组件实例序列化到 broker。启动消息只携带 `run_id` 与版本化 `LoopRequest` DTO；恢复消息只携带 `run_id` 与 `ResumeMode` 字符串。所有运行依赖在 Worker 执行时由调用方工厂创建。

## 推送语义，不是主动拉取 QueueBackend

Celery 自己管理 broker 消息确认、可见性和重新投递。因此：

- `CeleryQueueProducer` 实现 `matterloop_runtime.QueueProducer`；
- `CeleryQueueBackend` 只是该推送适配器的兼容名称；
- 两者都刻意不实现主动拉取 `QueueBackend.lease/acknowledge/release`；
- Worker 的消息所有权来自 Celery，MatterLoop 的业务执行所有权来自共享 `RunRepository.compare_and_set()`。

不要用 `isinstance(producer, QueueBackend)` 期待得到真，也不要在 Celery 外再启动 MatterLoop 拉取型 Worker 消费同一命令。

## 整体数据流

```text
API 进程
QueueRuntime.submit / resume
      ├─ RunRepository 创建或更新 QUEUED 记录
      └─ CeleryQueueProducer.enqueue
              └─ send_task(serializer="json", deterministic task_id)
                            │
                            ▼
Celery Worker
register_tasks 注册的同步任务函数
      └─ asyncio.run(_TaskProcessor)
              ├─ 严格解码 DTO
              ├─ 调用 factory 创建 CeleryWorkerDependencies
              ├─ RunRepository CAS: QUEUED -> RUNNING
              ├─ runtime.run / runtime.resume
              ├─ CAS 保存 LoopResult 与终止/暂停状态
              └─ closer.aclose()
```

确定性 Celery task id 便于撤销和 broker 诊断，但它本身不是幂等保证。真正阻止重复执行的是共享仓储的原子 CAS。

## 企业装配

```python
from matterloop_integration_celery import (
    CeleryQueueProducer,
    CeleryWorkerDependencies,
    register_tasks,
)
from matterloop_runtime import QueueRuntime

# API 进程：repository 必须与 Worker 使用同一个持久化实现。
producer = CeleryQueueProducer(celery_app, queue="matterloop")
runtime = QueueRuntime(producer, shared_run_repository)

# Worker 导入阶段只注册任务，不构造业务 Runtime。
register_tasks(celery_app, "my_project.worker:create_dependencies")


def create_dependencies() -> CeleryWorkerDependencies:
    return CeleryWorkerDependencies(
        runtime=create_async_runtime(),
        repository=create_shared_run_repository(),
        closer=create_optional_async_closer(),
        claim_lease_seconds=3600.0,
    )
```

工厂必须位于 Worker 可导入模块中，使用无参数 `模块:属性` 路径，并且每次任务调用都返回一个新的 `CeleryWorkerDependencies`。注册任务时只校验路径格式，不会导入或调用工厂。

## 公共 API

| 分组 | 公共类型或常量 |
| --- | --- |
| 生产者 | `CeleryQueueProducer`、`CeleryQueueBackend` |
| DTO 编解码 | `CeleryMessageCodec` |
| Worker 注册 | `CeleryWorkerDependencies`、`RegisteredCeleryTasks`、`register_tasks` |
| 最小协议 | `CeleryApp`、`CeleryControl`、`CeleryWorkerRuntime`、`AsyncCloser`、`CeleryTaskFunction` |
| 任务标识 | `RUN_TASK_NAME`、`RESUME_TASK_NAME`、`start_task_id`、`resume_task_id` |
| 异常 | `CeleryIntegrationError`、`CeleryPayloadError`、`CeleryFactoryError`、`CeleryRunConflictError`、`CeleryWorkerError` |

## `CeleryQueueProducer`

### 构造参数

| 参数 | 默认值 | 说明 |
| --- | --- | --- |
| `app` | 必填 | Celery 应用或满足 `CeleryApp` 的对象 |
| `queue` | `None` | 可选目标队列；提供时不能是空白字符串 |
| `codec` | `None` | 默认创建 `CeleryMessageCodec()` |

### `enqueue(job)`

`QueuedRun.action=START` 时：

- `job.request` 必须存在；
- 任务名为 `matterloop.run`；
- kwargs 仅为 `{"run_id": ..., "request": ...}`；
- task id 为 `matterloop:start:<run_id>`。

`QueuedRun.action=RESUME` 时：

- 任务名为 `matterloop.resume`；
- kwargs 仅为 `{"run_id": ..., "resume_mode": "continue|replan"}`；
- task id 为 `matterloop:resume:<mode>:<run_id>`。

两类任务固定使用 `serializer="json"`，并在配置 `queue` 时显式传给 `send_task`。同步 `send_task()` 通过线程执行，避免阻塞调用方事件循环。

### `cancel(run_id)`

取消会对以下三个确定性 id 依次调用 `app.control.revoke(task_id, terminate=False)`：

- 启动任务；
- continue 恢复任务；
- replan 恢复任务。

`run_id` 必须非空。成功提交撤销请求后返回 `True`，但这只是尽力取消：不会发送进程终止信号，也不能保证已经开始的任务停止。运行状态和 Core 取消仍由上层 `QueueRuntime`、共享仓储与实际 Runtime 协作完成。

## Celery 最小协议

### `CeleryApp`

| 方法/属性 | 签名语义 |
| --- | --- |
| `control` | 返回具有 `revoke(task_id, terminate=False)` 的 `CeleryControl` |
| `send_task` | `name, args=None, kwargs=None, **options` |
| `task` | `task(**options)` 返回任务装饰器 |

### `CeleryWorkerRuntime`

```python
async def run(
    request: LoopRequest,
    *,
    run_id: str | None = None,
) -> LoopResult: ...

async def resume(
    run_id: str,
    *,
    mode: ResumeMode = ResumeMode.CONTINUE,
) -> LoopResult: ...
```

### `AsyncCloser`

只要求 `async aclose() -> None`。若任务创建了多个资源，组合根应提供聚合 closer；本包不会自动发现或关闭 Runtime 内部资源。

## 版本化消息 DTO

`CeleryMessageCodec.schema_version` 当前固定为 `1`。解码使用精确字段集合，额外字段、缺失字段和其他版本都会拒绝。

### 启动任务外层 kwargs

| 字段 | 类型 | 必填 |
| --- | --- | --- |
| `run_id` | `str` | 是 |
| `request` | schema v1 对象 | 是 |

### request schema v1

| 字段 | 类型 | 约束 |
| --- | --- | --- |
| `schema_version` | `int` | 必须精确为 1，bool 不接受 |
| `goal` | `str` | 非空 |
| `acceptance_criteria` | `list[str]` | 每项非空 |
| `limits` | 对象 | 字段必须精确匹配下表 |
| `metadata` | JSON object | 字符串键，只允许标准 JSON 值，不允许 NaN/Infinity |

### limits 对象

| 字段 | 类型 | 约束 |
| --- | --- | --- |
| `max_cycles` | 正整数 | 至少为 1 |
| `max_attempts` | 正整数 | 至少为 1 |
| `max_steps_per_plan` | 正整数 | 至少为 1 |
| `timeout_seconds` | 数字或 `null` | 非 bool，提供时必须为有限数；最终仍由 `LoopLimits` 验证正值 |

编码会执行一次严格 JSON round-trip：tuple 会变为 list，非字符串键、对象实例、NaN 和 Infinity 会触发 `CeleryPayloadError`。消息不包含 pending interaction、Runtime、模型客户端、工具或 checkpoint；这些状态由共享仓储和 Worker 装配负责。

### 恢复任务 kwargs

| 字段 | 类型 | 约束 |
| --- | --- | --- |
| `run_id` | `str` | 对应共享仓储记录 |
| `resume_mode` | `str` | 必须可转换为 `ResumeMode.CONTINUE` 或 `ResumeMode.REPLAN` |

## Worker 依赖字段

### `CeleryWorkerDependencies`

| 字段 | 默认值 | 约束与所有权 |
| --- | --- | --- |
| `runtime` | 必填 | 必须满足 `CeleryWorkerRuntime` |
| `repository` | 必填 | 必须满足 `matterloop_runtime.RunRepository` |
| `closer` | `None` | 提供时必须满足 `AsyncCloser`；每次投递处理结束都调用 |
| `claim_lease_seconds` | `3600.0` | 必须大于 0；应严格大于单次任务正常最长执行时间 |

工厂返回类型会在运行时校验。工厂解析、调用或返回类型错误分别映射为 `CeleryFactoryError`，错误文本只包含路径或异常类型，不拼接工厂原始异常文本。

`closer` 位于 `finally` 中：正常完成、重复投递、业务异常都会执行。如果创建依赖后 closer 自身失败，该异常会传播给 Celery。工厂在返回 dependencies 之前失败时，本包无法关闭工厂内部已经部分创建的资源，因此工厂自身必须异常安全。

### `RegisteredCeleryTasks`

| 字段 | 含义 |
| --- | --- |
| `run` | 已注册的启动任务函数 |
| `resume` | 已注册的恢复任务函数 |

返回对象用于测试与 Worker 启动诊断；生产投递仍应通过任务名和 `CeleryQueueProducer`。

## `register_tasks`

| 参数 | 类型 | 必填 | 默认 | 业务含义 | 校验不变量 | 安全与持久化 |
| --- | --- | ---: | --- | --- | --- | --- |
| `celery_app` | `CeleryApp` | 是 | 无 | 注册启动与恢复任务的宿主 Celery 应用 | 必须满足最小任务注册协议 | 由宿主创建和关闭，不能携带到消息中 |
| `runtime_factory_path` | `str` | 是 | 无 | Worker 执行时解析的 `模块:无参工厂` | 模块与属性名均不得为空，只允许一个冒号 | 路径会进入 Worker 配置，不应包含凭据 |

```python
register_tasks(
    celery_app: CeleryApp,
    runtime_factory_path: str,
) -> RegisteredCeleryTasks
```

`runtime_factory_path` 必须为 `module:factory`，属性部分也可包含点路径，但不能包含第二个冒号。两个任务固定注册为：

| 任务 | Celery 选项 |
| --- | --- |
| `matterloop.run` | `acks_late=True`、`reject_on_worker_lost=True`、`serializer="json"` |
| `matterloop.resume` | `acks_late=True`、`reject_on_worker_lost=True`、`serializer="json"` |

任务函数是同步 Celery callable，内部用 `asyncio.run()` 创建单次事件循环。因此工厂必须为每个任务创建与该事件循环绑定的新异步资源，不能复用另一个事件循环中的客户端、锁或连接池。

## CAS 认领与幂等状态机

Worker 依赖的 `RunRepository` 必须提供：

```python
async def create(record: RunRecord) -> None: ...
async def get(run_id: str) -> RunRecord | None: ...
async def list(*, limit: int = 100, offset: int = 0) -> tuple[RunRecord, ...]: ...
async def compare_and_set(
    run_id: str,
    expected_version: int,
    replacement: RunRecord,
) -> bool: ...
```

生产实现必须持久化、跨进程共享，并保证 `compare_and_set` 真正原子。API 进程与全部 Worker 必须连接同一个逻辑仓储。

### 启动认领

1. Worker 解码消息；
2. 读取 `RunRecord`，不存在时抛 `RunNotFoundError`；
3. 把消息 request 与仓储 request 重新编码后精确比较，不一致抛 `CeleryRunConflictError`；
4. 仅 `QUEUED` 或超过 claim lease 的 `RUNNING` 可认领；
5. CAS 把 version 加一并设置 `RUNNING`、清空 error、更新时间；
6. CAS 失败视为重复/竞争，由当前仓储记录生成 duplicate 结果。

### 恢复认领

恢复消息不携带 request，只按 `run_id` 认领。上层 `QueueRuntime` 应先把可恢复记录转换为 `QUEUED` 再投递恢复命令。

### 完成

Runtime 返回的 `result.run_id` 必须等于认领记录 run id。允许持久化的 Loop 状态映射为：

| `LoopStatus` | `RunStatus` |
| --- | --- |
| `PAUSED` | `PAUSED` |
| `BLOCKED` | `BLOCKED` |
| `COMPLETED` | `COMPLETED` |
| `FAILED` | `FAILED` |
| `CANCELLED` | `CANCELLED` |
| `TIMED_OUT` | `TIMED_OUT` |

其他未稳定状态触发 `CeleryWorkerError`。完成时再次以认领 version CAS 保存完整 `LoopResult`；若 CAS 已被其他控制器推进，则返回当前记录并标记 duplicate，不覆盖新状态。

任务返回给 Celery result backend 的小型诊断对象字段固定为：

| 字段 | 含义 |
| --- | --- |
| `run_id` | 运行标识 |
| `status` | 当前 `RunStatus.value` |
| `version` | 当前仓储版本 |
| `duplicate` | 是否由重复/竞争投递返回 |

该对象不是权威业务结果；完整结果以共享 `RunRepository` 为准。

### 执行失败

Runtime 抛异常时，Worker 尝试 CAS 写入 `FAILED`，并只保存 `"<异常类型>: worker execution failed"`，不把供应商或业务异常文本写入仓储；随后原异常继续抛给 Celery，使其按 Worker 配置记录或重投。

## claim lease 与重复副作用

`claim_lease_seconds` 不是 broker visibility timeout，也没有心跳续租。它仅通过 `RunRecord.updated_at` 判断一个 `RUNNING` 记录是否陈旧。

如果正常任务执行时间超过该值，重新投递的 Worker 可能 CAS 接管并与旧 Worker 并发执行。最终状态 CAS 能避免旧结果覆盖新结果，但无法撤销已经发生的外部副作用。因此：

- 将 claim lease 配置为严格大于端到端最坏执行时间；
- 同时协调 Celery broker visibility timeout、任务 time limit 和 MatterLoop active timeout；
- Agent、工具和业务写操作必须使用 run/task id 做幂等键；
- 长时间任务若需要续租，应实现更完整的外部执行协调，本包当前没有 heartbeat API。

## 错误分类

| 异常 | 场景 |
| --- | --- |
| `CeleryPayloadError` | DTO 版本、字段、类型、JSON 值或领域限制无效 |
| `CeleryFactoryError` | 工厂路径无法解析、不可调用、调用失败或返回类型错误 |
| `CeleryRunConflictError` | 启动消息 request 与共享仓储 request 不一致 |
| `CeleryWorkerError` | resume mode 无效、时间无时区、run id 不匹配或 Runtime 返回未稳定状态 |
| `CeleryIntegrationError` | 本集成异常基类 |

共享 Runtime 还可能直接抛 `RunNotFoundError`、仓储异常、Celery broker 异常和底层 Runtime 异常。生产错误处理应按来源分类，不要把 broker 暂时失败误判为 Loop 业务失败。

## 生命周期与部署要求

- API 和 Worker 必须使用同一稳定 serializer 契约；当前只支持 schema v1，不提供旧版/新版兼容协商。
- Worker 进程启动时调用 `register_tasks()`，不要在每个任务中重复注册。
- Runtime 工厂每次消息执行才解析和调用，适合 Worker fork 后建立连接。
- 工厂返回的异步资源应由 `closer` 聚合关闭；生产连接池若希望进程级复用，需要自行提供与 Celery worker 生命周期集成的方案，不能复用单次 `asyncio.run()` 事件循环对象。
- `acks_late=True` 与 `reject_on_worker_lost=True` 会增加崩溃后的重新投递概率，这是预期行为；业务幂等必须独立成立。
- 本包不配置 Celery autoretry、retry backoff、rate limit、soft/hard time limit、broker TLS 或 result expiration；这些由宿主 Celery 配置负责。

## 敏感信息与安全边界

- 消息中的 goal、验收条件和 metadata 可能是敏感业务数据。broker、result backend 和监控系统必须配置 TLS、身份认证、ACL、保留周期和访问审计。
- 不要把 API Key、Cookie、Authorization、SDK 客户端或数据库凭据放入 `LoopRequest.metadata`。Codec 只保证 JSON 安全，不执行秘密扫描或加密。
- 仓储失败文本会脱敏，但 Runtime 原异常会重新抛给 Celery；Celery Worker 日志、Sentry 或 APM 仍可能记录原始异常文本，必须在宿主日志链路配置脱敏。
- `runtime_factory_path` 是受信任部署配置，不应来自消息、HTTP 参数或用户输入。
- JSON serializer 避免 pickle 对象执行风险；不要在 Celery 配置中为这些任务重新启用不可信 pickle payload。
- 确定性 task id 包含 `run_id`。run id 应是非敏感稳定标识，不要直接使用邮箱、订单明文或访问令牌。
- revoke 使用 `terminate=False`，不会强杀正在执行的模型、工具或数据库调用。

## 当前限制

- 只集成 Core Loop 的启动与恢复，不提供团队运行、人类反馈提交、事件查询或审批路由。
- 不实现 Celery canvas、group/chord、任务优先级、定时执行或任务级进度推送。
- 不提供 RunRepository 的持久化实现；必须由 Redis、SQL 等独立集成或应用实现。
- 没有 claim heartbeat；超长任务需要保守 lease 或外部协调。
- 不依赖 Celery result backend 保存完整 LoopResult，权威状态始终在共享 RunRepository。
- 取消仅为非终止 revoke 加上上层状态协作，不保证中断已运行任务。
- schema v1 不携带 Runtime、checkpoint 或人工交互内容，未来字段变化需要显式升级版本。
