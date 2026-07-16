# matterloop-runtime

Runtime 把 Loop 变成应用可以调用的服务边界：异步执行、同步桥接、队列控制面、组件生命周期和
本地进程执行。它不决定 Agent 如何规划，也不提供一个“安全运行任意代码”的沙箱。

```bash
pip install matterloop-runtime
```

## 三种入口

### 异步应用

```python
from matterloop_runtime import AsyncRuntime

async with AsyncRuntime(engine=agent_loop, resources=(model_client, tool_registry)) as runtime:
    result = await runtime.run(request)
```

`AsyncRuntime(engine, resources)` 逆序关闭 `resources`。不会因为某个对象被 `engine` 引用就自动取得
它的所有权。

### 同步应用

```python
from matterloop_runtime import LocalRuntime

with LocalRuntime(runtime=async_runtime, thread_name="matterloop-runtime") as runtime:
    result = runtime.run(request)
```

`LocalRuntime` 的 `runtime` 参数接收要桥接的异步门面，`thread_name` 默认为
`"matterloop-runtime"`。它使用专用事件循环线程，应长期复用并在进程退出前关闭；不要在 FastAPI
请求里临时创建，也不要从它自己的事件循环线程调用同步方法。

### 队列控制面

```python
from matterloop_runtime import QueueRuntime

runtime = QueueRuntime(
    producer=queue_producer,
    repository=run_repository,
    event_reader=event_reader,
)
run_id = await runtime.submit(request)
record = await runtime.wait(run_id, timeout_seconds=30)
```

`QueueRuntime(producer, repository, event_reader)` 负责 submit/get/list/wait/cancel/resume/result 和事件
查询，不执行任务。Worker 必须另行消费命令、调用 `AsyncRuntime`、通过仓储 CAS 提交结果，再 ack
或 release 消息。

## 人工反馈

Runtime 只转发 Core 语义：

```python
await runtime.submit_human_response(run_id, response)
result = await runtime.resume(run_id)  # 默认精确继续
```

提交反馈不会隐式恢复。需要重新规划时传 `ResumeMode.REPLAN`。

## 队列的两个并发边界

`QueueBackend` 的租约解决“哪个 Worker 暂时持有消息”，`RunRepository` 的 version CAS 解决“谁能
提交最新状态”。两者缺一不可。拿到租约不等于拥有外部副作用的全局唯一执行权。

- `QueueProducer` 只有 `enqueue/cancel`，适合 Celery 等推送系统。
- `QueueBackend` 还提供 `lease/acknowledge/release`，适合主动拉取 Worker。
- `RunRepository` 提供 `create/get/list/compare_and_set`。
- `RunEventReader` 用排他游标分页读取事件。

`InMemoryQueueBackend` 与 `InMemoryRunRepository` 只适合测试和单进程开发。过期租约在下一次
`lease()` 时回收，没有 heartbeat、死信队列或跨进程通知。

<details>
<summary>队列数据结构速查</summary>

- `QueuedRun(run_id, action, request, resume_mode, enqueued_at)`：`START` 必须携带 request，
  `RESUME` 不得携带 request。
- `QueueLease(lease_id, job, worker_id, expires_at, attempt)`：`attempt` 是消息交付次数，不是 Core
  Executor attempt。
- `RunRecord(run_id, request, status, version, result, error, created_at, updated_at)`：`version` 从 0
  开始，CAS replacement 必须严格加 1。
- `QueueAction`：`START`、`RESUME`。
- `RunStatus`：`QUEUED`、`RUNNING`、`PAUSED`、`BLOCKED`、`COMPLETED`、`FAILED`、`CANCELLED`、
  `TIMED_OUT`。

`wait()` 在 PAUSED/BLOCKED 也会返回，因为它们是 settled 状态；只有 completed/failed/cancelled/
timed_out 是终态。

</details>

## 安全热替换

`RuntimeContainer` 通过 `acquire(name)` 给长调用固定组件实例。`replace(name, component)` 会先启动
新实例，再让新调用看见它；旧实例等待既有租约退出后关闭。`get(name)` 只返回瞬时快照，不适合
跨 await 的长事务。

```python
async with container.acquire("model") as model:
    await model.generate(request)

await container.replace("model", replacement)
```

`register(name, component, replace=False)`、`unregister(name)`、`names()` 与 `aclose()` 组成完整生命
周期。构造器传入的初始组件视为已经启动。当前实现中，旧组件关闭失败时替换已经生效但异常仍会
传播；组件关闭应幂等，调用方收到错误后应查询容器状态再决定是否重试。

## 本地进程执行

```python
from matterloop_runtime import LocalProcessSandbox, ProcessRequest

sandbox = LocalProcessSandbox(
    root="/srv/workspaces/job-42",
    base_environment={"PATH": "/opt/matterloop/bin:/usr/bin"},
)
result = await sandbox.run(
    ProcessRequest(
        argv=("python", "-m", "pytest"),
        cwd=".",
        timeout_seconds=60,
        max_output_bytes=1_000_000,
    )
)
```

`ProcessRequest(argv, cwd, environment, stdin, timeout_seconds, max_output_bytes)` 直接传给
`create_subprocess_exec`，不使用 Shell；默认环境为空，默认超时 30 秒，stdout/stderr 共享
1,000,000 字节预算。`ProcessResult(return_code, stdout, stderr, duration_seconds, timed_out, truncated)`
说明执行结果与截断状态。

`LocalProcessSandbox` 只限制启动 cwd、环境、等待时间和保留输出。它不限制系统调用、网络、CPU、
内存、用户权限、可访问文件或子进程树；root 也不会阻止程序启动后读取目录外文件。不可信代码应
使用容器、虚拟机或远程沙箱实现 `Sandbox` 协议。

## 失败与关闭

关闭后的门面抛 `RuntimeClosedError`；重复 run ID 抛 `DuplicateRunError`；恢复不存在或不可恢复
运行分别抛 `RunNotFoundError`、`RunNotResumableError`；cwd 逃逸抛 `SandboxPathError`。

Runtime 不读取环境变量，也不创建 Redis、Celery、数据库或模型客户端。推荐关闭顺序是：停止新
请求，停止投递，排空或取消 Worker，释放租约，关闭 Runtime，最后关闭宿主持有的连接池。更多
部署约束见[企业集成指南](../docs/enterprise-integration.md)。
