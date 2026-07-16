简体中文 | [English](https://github.com/huleidada/matterloop/blob/main/matterloop-integration-redis/README.en.md)

# matterloop-integration-redis

这个包把 Redis 接到 MatterLoop 的队列、运行记录和事件流。它不把 Redis 变成“万能后端”：长期
记忆、Loop checkpoint 和 Worker 都不在这里。

```bash
pip install matterloop-integration-redis
```

## 三个适配器，三份职责

| 适配器 | 保存什么 | 关键保证 |
| --- | --- | --- |
| `RedisQueueBackend` | 待处理任务、延迟任务、租约和取消标记 | Lua 内的单节点原子状态转换；至少一次投递 |
| `RedisRunRepository` | `RunRecord` 与创建时间索引 | version CAS，防止并发覆盖 |
| `RedisEventPublisher` | 每个 run 的 Core 事件 Stream | 有序游标读取；近似长度裁剪 |

`RedisRunRepository` 不是 `CheckpointStore`。精确恢复所需的计划、当前步骤、人工反馈和 revision
必须保存到另一个持久化实现。

## 装配

```python
from matterloop_integration_redis import (
    RedisConfig,
    RedisEventPublisher,
    RedisQueueBackend,
    RedisRunRepository,
)

config = RedisConfig(prefix="matterloop:{prod}", lease_seconds=300)
queue = RedisQueueBackend(client=redis_client, config=config, codec=None)
runs = RedisRunRepository(client=redis_client, config=config, codec=None)
events = RedisEventPublisher(
    client=redis_client,
    config=config,
    checkpoint_codec=None,
)
```

`redis_client` 由应用创建并配置 TLS、认证、超时和连接池。三个适配器可以共享它，但都不会关闭它。

`RedisConfig(prefix, lease_seconds, event_max_length)` 只保存非敏感行为配置：默认值分别为
`"matterloop"`、`60.0` 秒和 `10_000` 条近似事件。连接 URL、用户名和密码不应进入配置对象。

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
<prefix>:runs:index             Sorted Set
<prefix>:runs:<run_id>          String (versioned JSON)
<prefix>:events:<run_id>        Stream
```

运行记录与事件没有 TTL 或删除 API。归档、删除、容量告警和数据保留属于部署方职责。

## Redis Cluster

队列脚本、仓储 CAS 和批量读取都会访问多个 Key，因此它们必须落在同一个 hash slot。默认前缀在
Cluster 中可能触发 `CROSSSLOT`；使用固定 hash tag，例如 `matterloop:{prod}`。这样会把这组数据
集中到一个 slot。需要横向分片时，请按租户配置多个前缀/客户端，或实现不同后端。

## 事件与敏感数据

`RedisEventPublisher` 会把事件时刻的完整 checkpoint 写入 Stream，里面可能包含目标、模型输出、
metadata 和人工反馈。本包不会自动调用 `Redactor`。生产环境应设置 Redis ACL、TLS、静态加密、
保留期限和单事件大小限制。

读取事件使用排他 Stream 游标 `after`。Stream 被裁剪后，旧游标不会报告缺口，只会从仍存在的
下一条开始；需要不可丢失审计时，不要依赖近似裁剪的 Redis Stream 作为唯一记录。

## 协议与错误

`AsyncRedisClient` 是最小结构协议，需要 `eval`、`get`、`mget`、`zrevrange`、`xadd`、`xrange`
和 `aclose`。`RedisPayloadCodec` 使用严格、版本化 JSON；未知版本或损坏内容抛
`RedisPayloadError`。网络、ACL、超时和 Cluster 错误由底层客户端原样传播，本包不自动重试。

构造入口为 `RedisQueueBackend(client, config, codec)`、`RedisRunRepository(client, config, codec)`
和 `RedisEventPublisher(client, config, checkpoint_codec)`。完整队列控制面和关闭顺序见
[企业集成指南](../docs/enterprise-integration.md)。
