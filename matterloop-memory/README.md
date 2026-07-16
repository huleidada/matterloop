# matterloop-memory

`matterloop-memory` 处理两件容易被混为一谈的事：给 Agent 检索历史信息，以及给 Loop 保存恢复点。
它只提供协议和进程内实现，不替你选择数据库。

```bash
pip install matterloop-memory
```

## 先分清两类数据

| 数据 | 接口 | 用途 | 是否进入任务恢复链路 |
| --- | --- | --- | --- |
| 长期记忆 | `MemoryStore` | 保存事实、经历和操作规则，供 Agent 检索 | 否 |
| Loop 检查点 | `CheckpointStore` | 保存状态机、当前步骤、人工反馈和 revision | 是 |

这两类数据的保留期、访问权限和一致性要求不同。生产环境不要把它们塞进同一张“通用记忆表”。

## 最小用法

```python
from matterloop_memory import (
    InMemoryCheckpointStore,
    InMemoryMemoryStore,
    MemoryKind,
    MemoryQuery,
    MemoryRecord,
)

memory = InMemoryMemoryStore()
checkpoints = InMemoryCheckpointStore()

await memory.put(
    MemoryRecord(
        namespace="tenant/acme/project/docs",
        kind=MemoryKind.SEMANTIC,
        content="发布前必须由独立验证器验收",
        metadata={"source": "engineering-policy"},
    )
)
matches = await memory.search(
    MemoryQuery(namespace="tenant/acme/project/docs", text="发布 验收", limit=5)
)
```

`NullMemoryStore` 用来明确关闭长期记忆，比在业务代码里传播 `None` 更容易测试。`InMemoryCheckpointStore`
可直接注入 `AgentLoop(checkpoint_store=...)`。

## 运行语义

- `namespace` 是查询条件，不是鉴权机制。应由可信身份派生，不能照搬客户端输入。
- `InMemoryMemoryStore` 使用简单词项交集评分，不是向量检索；`score` 不应与其他后端横向比较。
- 过期记录在读取时被隐藏，但不会被后台回收。长时间运行的服务必须使用带清理策略的实现。
- `InMemoryCheckpointStore.save()` 使用 revision CAS。冲突意味着状态已被其他执行者推进，应重新读取，
  不能直接覆盖。
- 所有内存实现只保证单进程内的一致性，进程退出后数据丢失。

## 接入持久化后端

自定义长期记忆只需实现结构协议 `MemoryStore`：

```python
class MemoryStore(Protocol):
    async def put(self, record: MemoryRecord) -> None: ...
    async def get(self, record_id: str) -> MemoryRecord | None: ...
    async def search(self, query: MemoryQuery) -> tuple[MemoryMatch, ...]: ...
    async def delete(self, record_id: str) -> bool: ...
    async def clear(self, namespace: str) -> int: ...
```

持久化检查点则实现 `matterloop_core.CheckpointStore`。至少要提供原子 CAS、租户隔离、加密、备份和
可审计删除；不要用长期记忆的相似度索引代替状态存储。

<details>
<summary>公共数据结构速查</summary>

- `MemoryKind`：`SEMANTIC`、`EPISODIC`、`PROCEDURAL`。
- `MemoryRecord(namespace, kind, content, metadata, record_id, created_at, expires_at)`：一条完整记忆；
  `record_id` 默认生成 UUID，`created_at` 默认使用当前 UTC，`expires_at=None` 表示不过期。
- `MemoryQuery(namespace, text, kinds, limit, min_score, filters)`：检索条件；`limit=10`、
  `min_score=0`，空 `kinds` 和空 `filters` 表示不追加过滤。
- `MemoryMatch(record, score)`：命中记录及后端给出的相关度。
- `InMemoryMemoryStore`、`NullMemoryStore`、`InMemoryCheckpointStore`：本包提供的三个实现。

`metadata`、`content` 和过滤值不会被自动脱敏。自定义后端应限制单条大小、可过滤字段和查询成本。

</details>

## 不在本包范围内

本包不提供 PostgreSQL、Redis、向量数据库、Embedding、跨进程锁或后台 TTL 清理。企业部署与数据
治理建议见[企业集成指南](../docs/enterprise-integration.md)，Loop 的恢复语义见
[架构说明](../docs/architecture.md)。
