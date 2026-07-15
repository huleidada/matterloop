# MatterLoop 企业离线示例

这四个示例只使用 Fake/内存组件，不读取环境变量、不需要密钥，也不连接模型、Redis、Celery
Broker 或其他外部服务。它们只通过各发行包的公共 API 装配，可作为企业组合根的可执行参考。

```bash
uv run python -m examples.enterprise.embedded_agent
uv run python -m examples.enterprise.team_collaboration
uv run python -m examples.enterprise.queued_service
uv run python -m examples.enterprise.mcp_skills_tools
```

| 示例 | 覆盖链路 | 离线替身 | 生产必须替换 |
| --- | --- | --- | --- |
| `embedded_agent.py` | FakeModel → Registry → Agent → Tool → Core → AsyncRuntime；暂停、修订、恢复、预算和审计 | FakeModel、内存记忆、内存 checkpoint、无副作用证据工具 | 供应商客户端、持久化 checkpoint、权限和审计 Publisher |
| `team_collaboration.py` | TeamPlanner → DAG fan-out/fan-in → AgentDirectory → Verifier → Reviewer → HITL | 确定性 Agent、内存 TeamRepository、内存事件 | 跨进程 CAS/lease 仓储、持久审计和受控 ArtifactStore |
| `queued_service.py` | FastAPI → QueueRuntime → lease/CAS → Worker → ack；Celery 与 Redis 两种接线 | 内存拉取队列、内存 RunRepository、记录型 Celery、禁止 I/O 的 Redis client | 只能选择一种任务传输；持久 RunRepository、CheckpointStore、审计、租约与清理策略 |
| `mcp_skills_tools.py` | MCP Session → MCP Registry → Tool Adapter → ToolRegistry；Skill Registry → SkillTool | 内存 MCP Session、不可变 Skill 内容 | 受控 transport/OAuth、正式 MCP Server、审核后的 Skill 根目录、租户权限与审计 |

`queued_service.py` 中的完整拉取流程使用 `InMemoryQueueBackend` 执行，是为了让 CI 可以验证
lease、CAS、Worker 和 acknowledge 的顺序。Celery 分支只验证 DTO 投递和任务注册；Redis 分支
只验证三个适配器共享宿主 client，任何 Redis 命令都会立即失败，防止示例意外联网。

示例不会展示真实凭据加载方式。企业应用应在自己的组合根中读取配置中心或密钥服务，构造好
SDK/Redis/Celery 客户端后再注入 MatterLoop，并在应用 lifespan 或 Worker shutdown 中关闭自有
资源。
