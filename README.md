# MatterLoop

MatterLoop 是面向可验证闭环任务的 Python 组件库。它将规划、执行、验证、审批、策略、模型、
工具、记忆、运行时和框架集成拆成独立发行包，可以按需安装，也可以通过 preset 快速装配。

- 支持 Python 3.10–3.14。
- 使用 uv virtual workspace 管理 12 个独立发行包。
- 每个发行包可独立构建、安装、测试和发布。
- 源码直接位于 `src/python/matterloop_xxx`，不存在 `src/python/matterloop/` 中间目录。
- 公共包提供 `py.typed`，并由包级 `__init__.py` 稳定导出公共 API。

## 发行包

| # | 发行包 | 导入名 | 主要职责 |
| ---: | --- | --- | --- |
| 1 | `matterloop-core` | `matterloop_core` | Loop 编排、上下文、状态机、事件、协议、检查点编码和组件注册 |
| 2 | `matterloop-models` | `matterloop_models` | 模型 DTO、协议、能力/租约注册表、测试实现与供应商适配子包 |
| 3 | `matterloop-runtime` | `matterloop_runtime` | 异步/同步门面、队列协议、运行仓储、本地进程沙箱和组件生命周期 |
| 4 | `matterloop-tools` | `matterloop_tools` | 工具协议、MCP/Skill 注册与适配、受限文件、Shell 和 HTTP 工具 |
| 5 | `matterloop-memory` | `matterloop_memory` | 长期记忆协议、空/内存记忆和内存检查点实现 |
| 6 | `matterloop-policies` | `matterloop_policies` | 预算、停止、重试、审批、权限策略和用量账本 |
| 7 | `matterloop-agents` | `matterloop_agents` | 单 Agent 组件与 TeamLoop/DAG 多智能体协作 |
| 8 | `matterloop-observability` | `matterloop_observability` | 结构化日志、脱敏、复合事件发布器和可选 OpenTelemetry |
| 9 | `matterloop-presets` | `matterloop_presets` | minimal、coding、research、production 运行时装配 |
| 10 | `matterloop-integration-fastapi` | `matterloop_integration_fastapi` | Loop HTTP Router、鉴权入口和错误映射 |
| 11 | `matterloop-integration-celery` | `matterloop_integration_celery` | Celery 任务注册、队列适配和幂等任务处理 |
| 12 | `matterloop-integration-redis` | `matterloop_integration_redis` | Redis 队列、运行仓储和事件发布适配器 |

## 依赖原则

`matterloop-core` 不导入任何其他 MatterLoop 包；`matterloop-models` 同样可以独立安装。
`matterloop_models.providers` 只依赖同一发行包内的模型抽象，抽象层不能反向导入供应商实现。
其余包只沿下面的方向依赖：

```text
runtime       -> core
tools         -> runtime
memory        -> core
observability -> core
policies      -> core + models + tools
agents        -> core + memory + models + tools
presets       -> agents + core + memory + models + observability + policies + runtime + tools
integration-* -> core + runtime + 对应第三方框架
```

`matterloop-policies` 通过模型协议提供非侵入式额度代理；Agent 与 preset 的运行时代码也只依赖
模型协议，由应用组合根按需从 `matterloop_models.providers` 导入具体适配器。preset 与
integration 是组合根，不允许底层模块反向依赖。
完整依赖边界见 [架构文档](docs/architecture.md)，跨模块装配、生产拓扑、资源所有权和数据治理见
[企业集成指南](docs/enterprise-integration.md)。

## 预设选择

- `build_minimal_runtime()`：基础 Agent、空工具注册表和内存检查点。
- `build_coding_runtime()`：只读默认执行器，以及需要审批的工作区写入与命令执行器。
- `build_research_runtime()`：只读文件、HTTPS `GET` 主机白名单和引用证据校验。
- `build_production_runtime()`：队列客户端与 worker runtime 组合，不提供内存基础设施回退。

生产预设必须显式传入 `QueueBackend`、`RunRepository`、`CheckpointStore` 和审计
`EventPublisher`。同步调用使用对应的 `build_*_local_runtime()`；本地同步门面由专用事件循环
线程驱动。

## 开发

```bash
uv sync --all-extras --dev
uv run ruff format --check .
uv run ruff check .
uv run mypy
uv run pytest
# 只有显式配置组合根后才会运行真实 DeepSeek 限额测试
uv run --env-file .env.local pytest -m live_deepseek
uv build --all-packages
```

各包的构造参数、公共字段、安全限制和最小示例位于对应发行包的 README。仓库还提供四个
[可运行企业离线示例](examples/enterprise/)，覆盖单 Agent、TeamLoop、队列服务以及
MCP/Skills/Tools 组合。

## 当前边界

- `LocalProcessSandbox` 只限制工作目录、环境、超时和输出量，不是恶意代码安全边界。
- 发行包不读取 `.env` 或进程环境；模型、Redis、队列、仓储等客户端由宿主应用构造并注入。
- OpenAI、DeepSeek、MiniMax、千问、智谱及通用 OpenAI-compatible Chat 适配器位于
  `matterloop_models.providers`；非兼容私有协议直接实现 `ModelClient` 或使用
  `CallableModelClient`。
- 默认测试不连接真实服务；`live_deepseek` 需要独立 opt-in、临时密钥和显式价格表。
- 当前不实现 PostgreSQL、向量数据库或其他持久化记忆后端。
- 当前不包含 CLI、Web 管理后台、Docker Compose、Kubernetes 或部署脚本。
