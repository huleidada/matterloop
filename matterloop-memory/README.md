# matterloop-memory

> 跨模块生产拓扑、资源所有权与上线检查见[企业集成指南](../docs/enterprise-integration.md)。

`matterloop-memory` 提供长期记忆协议、空实现、进程内实现，以及独立的 Loop 检查点内存存储。
它不会连接 PostgreSQL、向量数据库或其他外部持久化服务，也不会读取环境变量或创建外部
客户端。

长期记忆与 Loop 检查点是两条独立数据通道：`MemoryStore` 保存可检索的业务记忆，
`InMemoryCheckpointStore` 实现 `matterloop_core.CheckpointStore`，用于暂停、恢复和并发推进
Loop。两者不能共用表结构、保留策略或权限模型。

## 安装

```bash
pip install matterloop-memory
```

## 典型装配

```python
from matterloop_core import AgentLoop
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
        namespace="project:demo",
        kind=MemoryKind.SEMANTIC,
        content="验证通过后再结束循环",
        metadata={"source": "team-policy"},
    )
)
matches = await memory.search(
    MemoryQuery(namespace="project:demo", text="验证 结束", limit=5)
)

loop = AgentLoop(
    planners=planners,
    executors=executors,
    verifiers=verifiers,
    checkpoint_store=checkpoints,
    policy=policy,
    events=events,
    approval_gate=approval_gate,
    retry_policy=retry_policy,
)
```

生产环境应将 `MemoryStore` 和 `CheckpointStore` 分别替换为具有租户隔离、加密、审计和备份
能力的持久化实现。`InMemoryMemoryStore` 与 `InMemoryCheckpointStore` 仅适用于测试、示例和
单进程临时运行。

## 稳定公共入口

包级 `matterloop_memory.__all__` 导出以下 API：

| 分组 | 公共 API |
|---|---|
| 长期记忆 DTO | `MemoryKind`、`MemoryRecord`、`MemoryQuery`、`MemoryMatch` |
| 扩展协议 | `MemoryStore` |
| 长期记忆实现 | `NullMemoryStore`、`InMemoryMemoryStore` |
| Loop 检查点实现 | `InMemoryCheckpointStore` |

## 长期记忆字段

### `MemoryKind`

| 值 | 业务含义 |
|---|---|
| `semantic` | 稳定事实、概念或业务知识 |
| `episodic` | 一次任务、会话或事件经历 |
| `procedural` | 流程、操作方法或执行规则 |

### `MemoryRecord`

| 字段 | 类型 | 必填 | 默认 | 业务含义 | 校验不变量 | 安全与持久化 |
|---|---|---:|---|---|---|---|
| `namespace` | `str` | 是 | 无 | 租户、项目或会话隔离键 | 去除空白后不得为空 | 不会自动做租户授权；调用方必须使用不可碰撞的命名规则 |
| `kind` | `MemoryKind` | 是 | 无 | 记忆分类 | 源码不额外执行运行时类型校验 | 应作为可查询枚举持久化 |
| `content` | `str` | 是 | 无 | 记忆正文 | 去除空白后不得为空 | 默认会出现在对象 `repr` 且以内存明文保存，不得直接放入密钥 |
| `metadata` | `Mapping[str, str]` | 否 | `{}` | 来源、标签和业务索引 | 构造时复制并冻结顶层映射；不检查键值运行时类型 | 持久化实现应限制大小、字段白名单和敏感值 |
| `record_id` | `str` | 否 | 随机 UUID hex | 幂等覆盖和按标识读取的键 | 当前实现不拒绝空值 | 生产实现应建立唯一约束并验证标识格式 |
| `created_at` | `datetime` | 否 | 当前 UTC 时间 | 创建和稳定排序时间 | 调用方传入值当前不校验时区 | 持久化时应规范为带时区 UTC |
| `expires_at` | `datetime \| None` | 否 | `None` | 软过期时间 | 当前不校验时区或与创建时间的先后关系 | 内存实现只在读取和搜索时过滤，不主动清除过期记录 |

### `MemoryQuery`

| 字段 | 类型 | 必填 | 默认 | 业务含义 | 校验不变量 | 安全与持久化 |
|---|---|---:|---|---|---|---|
| `namespace` | `str` | 是 | 无 | 限定检索范围 | 去除空白后不得为空 | 它是过滤条件，不等于鉴权；服务端仍需绑定租户身份 |
| `text` | `str \| None` | 否 | `None` | 简单词项检索文本 | 空值或纯空白按“匹配全部”处理 | 不应把未经限制的用户输入写入审计日志 |
| `kinds` | `tuple[MemoryKind, ...]` | 否 | `()` | 可选分类过滤 | 空元组表示不过滤；当前不检查成员类型 | 持久化查询应使用参数绑定，不拼接查询语句 |
| `limit` | `int` | 否 | `10` | 最大返回数量 | 必须至少为 1；没有内建最大值 | 对外 API 应再设置租户级上限 |
| `min_score` | `float` | 否 | `0` | 最低相关度阈值 | 必须在 0 到 1 之间 | 内存评分不是向量相似度，不可跨后端比较绝对值 |
| `filters` | `Mapping[str, str]` | 否 | `{}` | metadata 精确匹配条件 | 构造时复制并冻结顶层映射 | 外部后端应限制可过滤字段与索引成本 |

### `MemoryMatch`

| 字段 | 类型 | 必填 | 默认 | 业务含义 | 校验不变量 | 安全与持久化 |
|---|---|---:|---|---|---|---|
| `record` | `MemoryRecord` | 是 | 无 | 命中的完整记忆 | 当前不额外校验 | 返回前应完成租户授权和字段脱敏 |
| `score` | `float` | 是 | 无 | 相关度 | DTO 本身不限制范围；内存实现产生 0 到 1 | 不建议持久化为跨算法稳定指标 |

## `MemoryStore` 协议

扩展实现无需继承基类，只需满足运行时可检查的结构协议：

| 方法 | 返回值 | 语义与要求 |
|---|---|---|
| `await put(record)` | `None` | 新增或按 `record_id` 替换记录；实现应定义原子性和幂等语义 |
| `await get(record_id)` | `MemoryRecord \| None` | 读取单条未过期记录；不存在时返回 `None` |
| `await search(query)` | `tuple[MemoryMatch, ...]` | 返回按相关度排序且不超过 `limit` 的不可变结果 |
| `await delete(record_id)` | `bool` | 返回删除前记录是否存在 |
| `await clear(namespace)` | `int` | 删除命名空间内记录并返回数量；生产实现应将其视为高风险操作 |

## 进程内实现的真实语义

### `InMemoryMemoryStore`

- 使用 `asyncio.Lock` 保护单进程内的读写；不提供跨进程或跨主机一致性。
- `put` 以 `record_id` 为键覆盖，即使新旧记录的 namespace 不同也会替换。
- `get` 和 `search` 会隐藏已过期记录，但不会回收它们占用的内存；`delete` 和 `clear` 仍可删除。
- 搜索先按 namespace、kind 和 metadata 精确过滤，再用大小写不敏感的 `\w+` 词项交集评分。
  `text=None`、空白或无法提取词项时得分为 `1.0`。
- 排序键为“得分降序、创建时间升序、record_id 升序”，结果具有确定性。

### `NullMemoryStore`

用于显式关闭长期记忆：写入被忽略，读取和搜索为空，删除返回 `False`，清理返回 `0`。
它适合将“禁用记忆”作为明确依赖注入，而不是在业务代码中散布 `None` 判断。

## `InMemoryCheckpointStore`

| 方法 | 参数与默认 | 返回值 | 并发语义 |
|---|---|---|---|
| `save(context, expected_revision=None)` | `context` 必填；省略 revision 时使用 `context.revision` | 提交后的新 revision | 当前存储 revision 必须等于期望值；成功严格加 1，否则抛 `CheckpointConflictError` |
| `load(run_id)` | `run_id` 必填 | `LoopContext \| None` | 返回 `snapshot()`，调用方修改不会直接污染存储 |
| `delete(run_id)` | `run_id` 必填 | `bool` | 原子删除并返回是否存在；它不是 Core `CheckpointStore` 协议的必需方法 |
| `list_run_ids()` | 无 | `tuple[str, ...]` | 返回排序后的稳定快照；它不是 Core 协议的必需方法 |

首次保存新运行时当前 revision 视为 `0`。`expected_revision` 不能为负数。同一 run 的并发推进
依靠 CAS 拒绝陈旧写入；调用方不得捕获冲突后盲目覆盖，而应重新加载并按业务语义合并。

## 生命周期、安全与企业边界

- 两个内存实现都没有 `start`、`aclose`、TTL 清理任务、容量限制、备份或恢复能力；进程退出即丢失。
- 数据、人工反馈、模型输出和 metadata 都以 Python 对象明文驻留；库不会加密、脱敏或审计访问。
- namespace 只是数据字段，不实施身份认证或租户授权。企业服务必须从可信身份派生 namespace，不能
  直接接受客户端任意指定。
- 自定义持久化实现应分别定义事务边界、唯一约束、CAS、过期清理、删除审计、加密、备份和数据驻留。
- 长期记忆可能包含提示注入或过期事实；使用前仍需来源校验、权限检查和业务验证。
- 本包不提供 PostgreSQL、Redis、向量检索、嵌入生成或 Loop 检查点的持久化后端。
