简体中文 | [English](README.en.md)

# 离线装配示例

这里的代码不是“Hello World”，而是四个可以直接运行的组合根。它们使用 Fake 或内存组件，不读
环境变量、不需要密钥，也不会连接模型服务、Broker 或 Redis。

```bash
uv run python -m examples.enterprise.embedded_agent
uv run python -m examples.enterprise.team_collaboration
uv run python -m examples.enterprise.queued_service
uv run python -m examples.enterprise.mcp_skills_tools
```

## 从哪个示例开始

| 你要解决的问题 | 示例 | 建议重点阅读 |
| --- | --- | --- |
| 在现有 Python 服务里运行一个可恢复 Agent | [`embedded_agent.py`](embedded_agent.py) | Runtime 装配、人工修订、预算与审计 |
| 把任务拆给多个 Agent 并做团队验收 | [`team_collaboration.py`](team_collaboration.py) | DAG、能力路由、fan-out/fan-in、Reviewer |
| 把 API 控制面与 Worker 分开 | [`queued_service.py`](queued_service.py) | lease、CAS、ack，以及 Celery/Redis 二选一 |
| 接入 MCP Server 和受控 Skill | [`mcp_skills_tools.py`](mcp_skills_tools.py) | Session 注入、工具授权、资源与 Prompt 的边界 |

每个示例都故意把依赖构造写在一起。生产项目通常会把它们放入 FastAPI lifespan、Worker 启动钩子
或自己的依赖注入容器，但资源所有权和关闭顺序应保持清晰。

## 替换离线组件

- `FakeModelClient` 换成 `matterloop_models.providers` 中的供应商适配器，SDK 客户端仍由应用创建。
- 内存 checkpoint、TeamRepository 和 RunRepository 换成带持久化、CAS、租约和备份的实现。
- 示例工具换成真实工具前，先接入 `ToolAuthorizer`、租户权限和审计；不要直接放开 Shell 或网络。
- Celery 是推送式任务传输，Redis `QueueBackend` 是拉取式任务传输。一个运行只选择其中一种。
- Redis 示例 client 会拒绝任何真实 I/O；它只用于验证装配关系，不能当成部署模板。

凭据加载属于宿主应用。请从配置中心或密钥服务取得凭据，构造外部客户端后注入 MatterLoop，并在
应用 shutdown 中关闭这些客户端。
