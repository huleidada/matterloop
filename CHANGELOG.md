简体中文 | [English](CHANGELOG.en.md)

# Changelog

本文件记录 MatterLoop 的用户可感知变化。仓库中的 12 个发行包采用同一版本，因此每个版本条目
覆盖整套组件，而不是分别维护互相漂移的变更日志。

## [Unreleased]

## [0.1.2] - 2026-07-23

### Added

- Failure Analysis Engine：按停止原因、验证反馈和错误模式归因，并生成可注入下一轮的纠正策略。
- Evaluation Framework：基准/黄金/回归任务集，以及 Agent、Runtime 与领域指标和评估循环。
- Learning Loop 与 `LoopEngineeringRuntime`：失败学习、策略优化、经验复用和多轮工程闭环。
- Agent Communication Model：Contract Schema 校验、消息总线与管理面注册表（能力、版本、SLA）。
- Memory 四层记忆：Working、Episodic、Semantic（向量与知识图谱）和 Procedural 参考实现。
- Event Bus、Event Router、生命周期处理器辅助与按 run/租户聚合的成本追踪。
- Execution Ledger、幂等调用、事务检查点与可水平扩展的 QueueWorker。
- MCP Governance：统一网关、风险分级策略、三维访问控制、配额与审计。
- 多租户隔离、令牌认证、角色授权与数据访问策略。

## [0.1.1] - 2026-07-21

### Added

- 全部公开 Markdown 新增英文镜像、双向语言切换和国际化契约测试。
- Core 长调用心跳、即时取消、崩溃恢复入口和 Redis 持久检查点。
- 队列租约续期、运行提交幂等和 CAS 终态保护。

### Changed

- 补齐 FastAPI `httpx2` 与 MCP 测试依赖，统一 12 个发行包的开发依赖与内部版本下限，并更新锁文件门禁。
- 执行结果在验证前写入检查点；状态不明确的执行默认进入对账阻塞，不再自动重放。

### Security

- 子 Agent 强制使用只读工具范围；Shell、写文件、非 GET HTTP 和未知 MCP 能力由主 Loop 统一治理。
- 工具副作用分类在注册中心授权前强制检查，业务 metadata 不能把子 Agent 提权为完整访问。

## [0.1.0] - 2026-07-16

### Added

- 可暂停、恢复、重规划和审计的 Agent Loop，包含结构化人工反馈与 checkpoint CAS。
- 基于 DAG 的 TeamLoop，多 Agent 能力路由、并行执行、独立验证和团队审查。
- 模型注册与供应商适配层，覆盖 OpenAI、DeepSeek、千问、智谱和 MiniMax，并保留自定义
  `ModelClient` 接口。
- 模型、工具、Agent 任务和估算费用的分层额度账本。
- MCP、Skills、Shell、文件系统与 HTTP 工具接入，以及审批和权限扩展点。
- 异步、本地同步、队列运行时与 FastAPI、Celery、Redis 集成包。
- minimal、coding、research、production 四套装配预设和企业离线示例。

### Security

- SDK 客户端和凭据由应用构造并注入，发行包不读取 `.env` 或保存 API key。
- 模型 continuation/reasoning 不进入公开结果，日志与事件支持敏感字段脱敏。
- Shell 工具使用 argv 调用，文件与 HTTP 工具提供路径、协议、host 和响应大小边界。

[Unreleased]: https://github.com/huleidada/matterloop/compare/v0.1.2...HEAD
[0.1.2]: https://github.com/huleidada/matterloop/releases/tag/v0.1.2
[0.1.1]: https://github.com/huleidada/matterloop/releases/tag/v0.1.1
[0.1.0]: https://github.com/huleidada/matterloop/releases/tag/v0.1.0
