# matterloop-integration-redis

> 跨模块生产拓扑、资源所有权与上线检查见[企业集成指南](../docs/enterprise-integration.md)。

本包提供 Redis 版 `QueueBackend`、`RunRepository` 和 Core 生命周期事件发布/读取器，用于
`QueueRuntime` 或 production preset 的基础设施装配。它不把 Redis 用作长期记忆或 Loop
`CheckpointStore`，也不包含 worker 循环、连接工厂或迁移命令。

Redis 客户端由宿主应用按自己的配置、TLS 和密钥体系显式构造。本包不读取环境变量，也不会
保存 URL、主机、用户名或密码。

## 公共 API

| 类型 | 角色 |
| --- | --- |
| `AsyncRedisClient` | 适配器需要的最小异步客户端结构协议 |
| `RedisConfig` | 仅保存非敏感 Key、租约和 Stream 保留配置 |
| `RedisPayloadCodec` | 队列命令与运行记录的版本化严格 JSON 编解码器 |
| `RedisQueueBackend` | Lua 原子队列、延迟重排、取消和租约回收 |
| `RedisRunRepository` | 运行记录、创建时间索引和版本 CAS |
| `RedisEventPublisher` | 按 run 隔离的 Stream 事件发布器，同时实现 `RunEventReader` |
| `RedisIntegrationError` | Redis 集成异常基类 |
| `RedisPayloadError` | Redis 返回值或持久化载荷不符合 Schema |

`AsyncRedisClient` 需要 `eval/get/mget/zrevrange/xadd/xrange/aclose`。适配器接受 Redis 返回
`str` 或 `bytes`，但不会调用客户端的 `aclose()`；连接池始终归宿主所有。

## 配置与装配

| `RedisConfig` 字段 | 默认值 | 约束与用途 |
| --- | ---: | --- |
| `prefix` | `"matterloop"` | 只能包含字母、数字、`_ . : { } -`；隔离所有 Key |
| `lease_seconds` | `60.0` | 正有限数且大于 0；`lease()` 未显式传值时使用 |
| `event_max_length` | `10_000` | 至少 1；作为 Stream 近似 `MAXLEN` |

三个适配器的构造参数必须由应用显式提供：

| 适配器.参数 | 类型 | 必填 | 默认 | 业务含义 | 校验不变量 | 安全与持久化 |
| --- | --- | ---: | --- | --- | --- | --- |
| `RedisQueueBackend.client` / `RedisRunRepository.client` / `RedisEventPublisher.client` | `AsyncRedisClient` | 是 | 无 | 宿主创建的共享异步连接 | 必须满足最小 Redis 协议 | 适配器不关闭 client，凭据只存在于宿主客户端 |
| `RedisQueueBackend.config` / `RedisRunRepository.config` / `RedisEventPublisher.config` | `RedisConfig \| None` | 否 | `None` | Key 前缀、租约和事件保留行为 | 空值构造默认配置 | 配置不允许保存连接信息 |
| `RedisQueueBackend.codec` / `RedisRunRepository.codec` | `RedisPayloadCodec \| None` | 否 | `None` | 队列和运行记录 schema v1 编解码器 | 自定义实现必须保持严格版本边界 | 载荷可能含目标、输出和 metadata |
| `RedisEventPublisher.checkpoint_codec` | `LoopCheckpointCodec \| None` | 否 | `None` | Core 事件上下文的 schema v2 编解码器 | 空值创建官方 codec | 事件 Stream 会保存完整 checkpoint，必须加密和限权 |

```python
from matterloop_integration_redis import (
    RedisConfig,
    RedisEventPublisher,
    RedisQueueBackend,
    RedisRunRepository,
)
from matterloop_presets import build_production_runtime

# application_redis_client 由宿主显式创建；建议启用 TLS、连接超时和命令超时。
client = application_redis_client
config = RedisConfig(prefix="matterloop:prod", lease_seconds=300)

queue = RedisQueueBackend(client, config)
runs = RedisRunRepository(client, config)
events = RedisEventPublisher(client, config)

runtime = build_production_runtime(
    model_client,
    queue_backend=queue,
    run_repository=runs,
    checkpoint_store=durable_checkpoint_store,
    audit_publisher=events,
    event_reader=events,
)
```

事件 Publisher 与运行仓储是两套独立数据。`durable_checkpoint_store` 仍必须由应用提供；把
`RedisRunRepository` 当作 Core 检查点仓储会丢失精确恢复语义。

## Key 布局

以下 `<prefix>` 为配置前缀，`<run_id>` 为运行标识：

| Key | Redis 类型 | 内容 |
| --- | --- | --- |
| `<prefix>:queue:pending` | List | 可立即租用的 run ID |
| `<prefix>:queue:delayed` | Sorted Set | 延迟至时间戳的 run ID |
| `<prefix>:queue:jobs` | Hash | run ID → 队列命令 JSON |
| `<prefix>:queue:attempts` | Hash | run ID → 当前尝试次数 |
| `<prefix>:queue:leases` | Hash | lease ID → 租约 JSON |
| `<prefix>:queue:lease-expiry` | Sorted Set | lease ID → 到期时间戳 |
| `<prefix>:queue:run-leases` | Hash | run ID → 当前 lease ID |
| `<prefix>:queue:cancelled` | Set | 已请求取消的 run ID |
| `<prefix>:runs:index` | Sorted Set | run ID → 创建时间戳 |
| `<prefix>:runs:<run_id>` | String | `RunRecord` JSON |
| `<prefix>:events:<run_id>` | Stream | 字段 `payload` 保存完整事件 JSON |

队列在有效 acknowledge 后删除 job、attempt、lease 和取消标记；运行记录、索引及事件 Stream
没有 TTL 或删除 API。企业部署必须自行定义数据保留、归档、删除和 Redis 内存告警。

## 队列与租约语义

`RedisQueueBackend` 每次直接执行 Lua `EVAL`，多 Key 状态转换在单个 Redis 主节点内原子完成：

- `enqueue(job)`：同一 jobs Hash 中已有 run ID 时抛 `DuplicateRunError`，否则写 job、把 attempt
  设为 1、清除旧取消标记并追加 pending；
- `lease(worker_id, lease_seconds=None)`：先把到期 delayed 命令移回 pending，再回收已过期租约，
  然后租出一个 job；队列为空返回 `None`；
- `release(lease, delay_seconds=0)`：有效租约的 attempt 加一，立即进入 pending 或进入 delayed；
- `acknowledge(lease)`：删除有效租约及对应 job；未知、过期或不匹配租约是安全空操作；
- `cancel(run_id)`：等待中的 job 会被清理；已租用 job 先记录取消标记，待 release 或租约过期时
  清理。运行中的用户代码不会被 Redis 主动中断。

过期租约只在后续 `lease()` 调用时被回收。本协议没有 heartbeat/renew 方法，也没有最大尝试、
死信队列或自动退避；任务若可能超过租约时间，应设置覆盖最坏执行时长的租约，或在业务层拆分
任务。worker 崩溃、网络超时或租约过期均可能导致重复执行，因此这是**至少一次**投递，不是
恰好一次。外部副作用必须使用幂等键，运行状态必须通过 `RunRepository.compare_and_set()` 提交。

租约和延迟判断使用调用方进程的 UTC 时钟，不调用 Redis `TIME`。多主机应保持时钟同步，并把
时钟漂移纳入租约裕量。

## 运行仓储与 CAS

`RedisRunRepository.create()` 用 Lua 同时创建 `<prefix>:runs:<run_id>` 和索引成员，重复 run ID
抛 `DuplicateRunError`。`list(limit=100, offset=0)` 按创建时间倒序从索引取 ID，再用 `MGET`
读取记录；这两个步骤不是同一个快照，期间并发更新可见，索引存在但记录缺失时会跳过该项。

`compare_and_set(run_id, expected_version, replacement)` 要求：

- `expected_version >= 0`；
- replacement 的 run ID 与目标一致；
- replacement.version 恰好为 `expected_version + 1`；
- 当前记录版本等于 expected version；
- replacement 必须保留原始 `created_at`。

版本不匹配或记录不存在返回 `False`；损坏 JSON、CAS Schema 不合法或创建时间被改写会抛
`RedisPayloadError`。CAS 只能防止状态覆盖，不能撤销已经发生的外部工具副作用。

## 事件 Stream

`RedisEventPublisher.publish()` 写入 `schema_version/event_type/run_id/occurred_at/detail/checkpoint`，
其中 checkpoint 是事件时刻的完整 `LoopContext`。`XADD` 使用 `approximate=True`，因此
`event_max_length=10_000` 是近似上限，实际条数可短时超过。

`list_events(run_id, after=None, limit=100)` 按 Stream ID 正序返回事件，并追加可信的 `event_id`
字段。`after` 是排他游标，格式必须为 `<毫秒>-<序号>`；载荷版本、事件类型、时区、run ID 和
checkpoint 都会严格验证。Stream 被裁剪后，旧游标不会报“已丢失”，只会从仍存在的下一条开始。

事件中保存完整请求、metadata、反馈和执行上下文，可能包含个人信息或业务秘密；本适配器不会
自动调用 `matterloop-observability.Redactor`。发布前应限制进入上下文的数据，配置 Redis ACL、
TLS、静态加密和保留期限，并评估完整 checkpoint 对网络和内存的放大效应。

## Payload Schema

`RedisPayloadCodec.schema_version` 当前为 `1`，内部复用 Core `LoopCheckpointCodec`。它只写严格
JSON，不序列化 Python 对象，禁止 NaN，并检查 job、record、result、request 和 run ID 的一致性。
未知 schema version、非 UTF-8、无时区时间、未知枚举或损坏 checkpoint 都会抛
`RedisPayloadError`。

当前没有跨 Redis payload schema 的迁移器。滚动升级前应验证新旧进程可读取同一数据；发生不兼容
变更时，需要先排空队列并迁移或隔离 prefix。不要手工修改 jobs、records 或 Stream payload。

## Redis Cluster 限制

队列 Lua 最多同时访问 8 个 Key，仓储 create/CAS 同时访问记录 Key 与索引，`list()` 还会对多个
记录执行 `MGET`。Redis Cluster 要求这些 Key 位于同一个 hash slot。默认
`prefix="matterloop"` **不满足**该约束，Cluster 上会出现 `CROSSSLOT`。

如确需 Redis Cluster，应在 prefix 中使用固定且非空的 hash tag，例如：

```python
config = RedisConfig(prefix="matterloop:{prod}")
```

这样本集成的所有 Key 都落到 `{prod}` 对应的单一 slot，能够执行 Lua 和 MGET，但也意味着所有
队列、运行记录和事件集中到同一分片，无法水平分散热点。需要跨 slot 扩展时，当前适配器不适用，
应按租户/队列建立多个独立 prefix 与客户端，或实现不依赖多 Key 原子脚本的后端。

## 失败、安全与运维

- Redis 网络、ACL、超时和 `CROSSSLOT` 等客户端异常原样传播；本包不重试。重试策略必须区分
  可安全重放的读操作与可能已经提交的 Lua/XADD。
- 服务端必须支持 Lua/cjson 与 Redis Stream（Redis 5.0+ 或具有同等命令语义的兼容服务）；上线前
  应在目标托管服务验证 `EVAL`、`XADD/XRANGE`、Sorted Set 和 ACL 限制。
- run ID 会进入 Redis Key 和 Hash 字段；应使用受控、长度有限的内部标识，不直接接受任意用户
  文本，并通过不同 prefix 做环境和租户隔离。
- 不要向普通业务角色授予 `EVAL`、全库扫描或任意 Key 权限；Redis ACL 应只开放目标 prefix 和
  所需命令。
- 上线前应压测 Lua 延迟、单 slot 热点、Stream payload 大小、连接池上限和故障切换行为。
- 优雅关闭顺序是：停止 API 投递，等待或取消 worker，处理/释放租约，关闭 MatterLoop runtime，
  最后由宿主调用共享 Redis 客户端的 `aclose()`。
