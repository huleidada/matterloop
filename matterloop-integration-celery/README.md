# matterloop-integration-celery

Celery 已经拥有 Broker、消息确认和重新投递语义。这个包只负责两端的接线：API 进程发送版本化
MatterLoop 命令，Worker 进程重建 Runtime，并用共享 `RunRepository` 认领运行。

```bash
pip install matterloop-integration-celery
```

## 它不是拉取式 QueueBackend

`CeleryQueueProducer` 实现 `QueueProducer`。兼容名称 `CeleryQueueBackend` 指向同一个推送适配器，
但它没有 `lease/acknowledge/release`。不要再启动 MatterLoop 拉取 Worker 消费同一批命令。

```text
API / QueueRuntime
  ├─ 先把 RunRecord 写入共享仓储
  └─ CeleryQueueProducer.send_task(JSON)
                 │
                 ▼
Celery Worker
  ├─ 严格解码消息
  ├─ 调用工厂创建 Runtime 与仓储
  ├─ CAS: QUEUED → RUNNING
  ├─ runtime.run() / runtime.resume()
  └─ CAS: RUNNING → 终态或 PAUSED
```

Celery 的确定性 task id 便于撤销和排障，不能替代仓储 CAS。

## API 进程

```python
from matterloop_integration_celery import CeleryQueueProducer
from matterloop_runtime import QueueRuntime

producer = CeleryQueueProducer(app=celery_app, queue="matterloop", codec=None)
runtime = QueueRuntime(producer=producer, repository=shared_repository)
```

`CeleryQueueProducer(app, queue, codec)` 对同步 `send_task()` 使用线程封装，避免阻塞事件循环。启动任务
名是 `matterloop.run`，恢复任务名是 `matterloop.resume`，消息固定使用 JSON serializer。

`cancel(run_id)` 会对启动、continue 恢复和 replan 恢复三个确定性 task id 发出
`revoke(terminate=False)`。返回成功只表示撤销请求已提交，不代表正在执行的代码已经停止。

## Worker 进程

```python
from matterloop_integration_celery import (
    CeleryWorkerDependencies,
    register_tasks,
)

register_tasks(
    celery_app=celery_app,
    runtime_factory_path="my_project.worker:create_dependencies",
)


def create_dependencies() -> CeleryWorkerDependencies:
    return CeleryWorkerDependencies(
        runtime=create_runtime(),
        repository=create_shared_repository(),
        closer=create_closer(),
        claim_lease_seconds=3600.0,
    )
```

`register_tasks(celery_app, runtime_factory_path)` 只注册两个同步 Celery task。工厂路径必须是受信任的
`module:callable` 配置，不能来自请求或消息。每次投递都会在新的 `asyncio.run()` 事件循环中调用
工厂，因此不要返回绑定到旧事件循环的连接、锁或客户端。

工厂必须返回 `CeleryWorkerDependencies(runtime, repository, closer, claim_lease_seconds)`；
`claim_lease_seconds` 默认 3600 秒。`closer` 会在正常完成、重复投递和异常后执行。返回的
`RegisteredCeleryTasks(run, resume)` 主要用于启动诊断和测试。

## 消息边界

启动消息只包含 `run_id` 和 schema v1 的 `LoopRequest`：goal、验收条件、limits 与 JSON metadata。
恢复消息只包含 `run_id` 和 `continue/replan`。Runtime、模型、工具、客户端、checkpoint 和人工交互
对象都不会进入 Broker。

`CeleryMessageCodec` 拒绝未知字段、未知 schema、NaN、Infinity 和任意 Python 对象。JSON 安全不
等于数据无敏感性；goal 与 metadata 仍需要 Broker TLS、ACL、保留期限和日志脱敏。

## 重复投递如何处理

共享 `RunRepository.compare_and_set()` 是执行所有权的唯一判据：

1. Worker 读取 `RunRecord`，确认消息与仓储中的 request 一致。
2. 只有 `QUEUED` 或超过 claim lease 的 `RUNNING` 可以被 CAS 认领。
3. Runtime 返回后，再以认领版本 CAS 保存结果。
4. CAS 失败说明其他控制器已经推进状态；当前 Worker 返回 duplicate 诊断，不覆盖新状态。

claim lease 只比较 `RunRecord.updated_at`，没有 heartbeat。若任务正常耗时超过该值，另一个 Worker
可能接管并与旧 Worker 并发。最终 CAS 能防止状态覆盖，但无法撤销邮件、支付、文件写入等外部
副作用。工具和业务写操作必须用 run/task id 做幂等键。

同时协调四个时间边界：claim lease、Broker visibility timeout、Celery time limit 和
`LoopLimits.timeout_seconds`。claim lease 应覆盖端到端最坏执行时间和时钟漂移。

## 失败与所有权

- 无效 DTO 抛 `CeleryPayloadError`；工厂解析或返回类型错误抛 `CeleryFactoryError`。
- 消息 request 与仓储 request 不一致抛 `CeleryRunConflictError`。
- Runtime 失败时，Worker 尝试把记录 CAS 为 `FAILED`，仓储只写异常类型和固定摘要；原异常仍会
  抛给 Celery，宿主日志链路必须继续脱敏。
- API 与全部 Worker 必须使用同一个持久化仓储。进程内仓储不满足这个条件。
- 本包不关闭 Celery app 或共享基础设施；只关闭工厂显式返回的 `closer`。

任务注册启用 late ack 和 worker-lost 重投，这是有意的至少一次语义。本包不配置 autoretry、
backoff、rate limit、soft/hard time limit、Broker TLS 或 result expiration。

## 当前边界

当前只传输 Core Loop 的启动与恢复；没有团队运行、提交人工反馈、事件查询、claim heartbeat 或
持久化仓储实现。完整 `LoopResult` 以共享 `RunRepository` 为准，不依赖 Celery result backend。
部署组合与关闭顺序见[企业集成指南](../docs/enterprise-integration.md)。
