简体中文 | [English](https://github.com/huleidada/matterloop/blob/main/matterloop-integration-redis/README.en.md)

# matterloop-integration-redis

这个包为 MatterLoop 提供持久化 Loop 检查点、任务队列、运行记录和事件流。它不把 Redis
变成“万能后端”：长期记忆和 Worker 仍由应用选择并装配。

```bash
pip install matterloop-integration-redis
```

## 四个适配器，四份职责

| 适配器 | 保存什么 | 关键保证 |
| --- | --- | --- |
| `RedisCheckpointStore` | 完整 `LoopContext` 检查点 | revision CAS；严格的 Core checkpoint schema |
| `RedisQueueBackend` | 待处理任务、延迟任务、租约和取消标记 | Lua 内的单节点原子状态转换；至少一次投递 |
| `RedisRunRepository` | `RunRecord` 与创建时间索引 | version CAS，防止并发覆盖 |
| `RedisEventPublisher` | 每个 run 的 Core 事件 Stream | 有序游标读取；近似长度裁剪 |

`RedisCheckpointStore` 保存精确恢复所需的计划、步骤游标、人工反馈、待验证执行结果和 revision。
`RedisRunRepository` 保存给控制面查询的状态与结果；两者数据用途不同，不能相互替代。

## 装配

```python
from matterloop_integration_redis import (
    RedisCheckpointStore,
    RedisConfig,
    RedisEventPublisher,
    RedisQueueBackend,
    RedisRunRepository,
)

config = RedisConfig(prefix="matterloop:{prod}", lease_seconds=300)
checkpoints = RedisCheckpointStore(client=redis_client, config=config, codec=None)
queue = RedisQueueBackend(client=redis_client, config=config, codec=None)
runs = RedisRunRepository(client=redis_client, config=config, codec=None)
events = RedisEventPublisher(
    client=redis_client,
    config=config,
    checkpoint_codec=None,
)
```

`redis_client` 由应用创建并配置 TLS、认证、超时和连接池。四个适配器可以共享它，但都不会关闭它。

`RedisConfig(prefix, lease_seconds, event_max_length)` 只保存非敏感行为配置：默认值分别为
`"matterloop"`、`60.0` 秒和 `10_000` 条近似事件。连接 URL、用户名和密码不应进入配置对象。

## 检查点字段与 CAS 语义

| API / 字段 | 类型 | 必填 | 默认值 | 业务含义 | 校验与持久化 |
| --- | --- | --- | --- | --- | --- |
| `RedisCheckpointStore.client` | `AsyncRedisClient` | 是 | 无 | 执行 `GET` 与 Lua CAS | 由宿主管理连接，不进入 payload |
| `RedisCheckpointStore.config` | `Optional[RedisConfig]` | 否 | `RedisConfig()` | 提供 Key 前缀 | 不保存连接信息或凭据 |
| `RedisCheckpointStore.codec` | `Optional[LoopCheckpointCodec]` | 否 | `LoopCheckpointCodec()` | 编解码 Core 当前 checkpoint 结构 | 非法字段严格失败 |
| `save.context` | `LoopContext` | 是 | 无 | 要提交的完整隔离快照 | 以 `<prefix>:checkpoints:<run_id>` 保存为 JSON String |
| `save.expected_revision` | `Optional[int]` | 否 | `context.revision` | 调用方读取到的 CAS 版本 | 必须是非负整数；新建必须为 `0` |
| `load.run_id` | `str` | 是 | 无 | 要恢复的稳定运行标识 | 空值被拒绝；payload 的 `run_id` 必须与 Key 一致 |

`save()` 在一段 Lua 中读取当前值、校验 schema 与 `run_id`、比较 revision，再写入新快照。成功
返回 `expected_revision + 1`，但不会原地修改调用方的 `context.revision`；控制器应采用返回值。
版本不一致抛 `CheckpointConflictError`，损坏 JSON、非法 UTF-8、未知 checkpoint schema 或异常
Redis 返回值抛 `RedisPayloadError`。`load()` 对不存在的 Key 返回 `None`。

Redis 命令超时具有“结果未知”语义：Lua 可能已经提交。调用方不能直接重放副作用，应先重新
`load()` 并核对 revision。检查点没有 TTL、列举或删除 API，也不与 `RunRecord` 或事件 Stream
形成跨 Key 事务；数据保留、清理和 Outbox 由部署方负责。

## 队列不是“恰好一次”

Worker 调用 `lease()` 取得任务，成功后 `acknowledge()`，可重试失败则 `release()`。租约过期的任务
只会在下一次 `lease()` 时被回收；当前接口没有续租、最大尝试或死信队列。执行时间可能超过租约
时，要么提高租约并预留时钟漂移，要么把任务拆小。

网络超时发生时，Lua 可能已经提交。外部写操作必须带幂等键，最终运行状态必须通过
`RunRepository.compare_and_set()` 提交。`cancel()` 只能阻止等待中的任务或标记租用中的任务，
不会中断已经运行的 Python 代码。

## Key 布局

所有 Key 都位于 `<prefix>` 下：

```text
<prefix>:queue:pending          List
<prefix>:queue:delayed          Sorted Set
<prefix>:queue:jobs             Hash
<prefix>:queue:leases           Hash
<prefix>:queue:lease-expiry     Sorted Set
<prefix>:checkpoints:<run_id>   String (Core 当前 checkpoint 结构，revision CAS)
<prefix>:runs:index             Sorted Set
<prefix>:runs:<run_id>          String (versioned JSON)
<prefix>:events:<run_id>        Stream
```

检查点、运行记录与事件都没有 TTL 或删除 API。归档、删除、容量告警和数据保留属于部署方职责。

## Redis Cluster

队列脚本、仓储 CAS 和批量读取会访问多个 Key，因此它们必须落在同一个 hash slot；检查点 CAS
只访问自身的一个 Key。默认前缀在 Cluster 中可能触发 `CROSSSLOT`，应使用固定 hash tag，例如
`matterloop:{prod}`。需要横向分片时，请按租户配置多个前缀/客户端，或实现不同后端。

## 事件与敏感数据

`RedisCheckpointStore` 和 `RedisEventPublisher` 都可能保存目标、模型输出、metadata、工具结果和
人工反馈。本包不会自动调用 `Redactor`。生产环境应设置 Redis ACL、TLS、静态加密、保留期限、
租户隔离和 payload 大小限制。

读取事件使用排他 Stream 游标 `after`。Stream 被裁剪后，旧游标不会报告缺口，只会从仍存在的
下一条开始；需要不可丢失审计时，不要依赖近似裁剪的 Redis Stream 作为唯一记录。

## 协议与错误

`AsyncRedisClient` 是最小结构协议，需要 `eval`、`get`、`mget`、`zrevrange`、`xadd`、`xrange`
和 `aclose`。适配器只等待这些异步方法，不读取连接配置。`RedisPayloadCodec` 与
`LoopCheckpointCodec` 使用严格、版本化 JSON；未知版本或损坏内容抛 `RedisPayloadError`。
网络、ACL、超时和 Cluster 错误由底层客户端原样传播，本包不自动重试。

构造入口为 `RedisCheckpointStore(client, config, codec)`、
`RedisQueueBackend(client, config, codec)`、`RedisRunRepository(client, config, codec)` 和
`RedisEventPublisher(client, config, checkpoint_codec)`。完整队列控制面和关闭顺序见
[企业集成指南](../docs/enterprise-integration.md)。
