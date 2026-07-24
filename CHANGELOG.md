简体中文 | [English](CHANGELOG.en.md)

# Changelog

本文件记录 MatterLoop 的用户可感知变化。仓库中的 12 个发行包采用同一版本，因此每个版本条目
覆盖整套组件，而不是分别维护互相漂移的变更日志。

## [Unreleased]

### Added

- Observability 新增树形 tracing 与评分：`TraceBuilder` 把生命周期事件流重建为跨度树，
  `BatchingPipeline` 聚批导出 `SpanRecord` 与 `Score`，提供 JSONL 与 OTLP/HTTP 两种导出器；
  `TracedModelClient` 包装模型客户端自动记录 generation 跨度。
- production preset 新增可选 `trace_exporter` 参数，一键把 TraceBuilder 挂入审计事件管线并包装模型
  客户端，导出流水线随 runtime 关闭自动排空。

### Deprecated

- `TracingHandler` 标记废弃，孤立短跨度由 `TraceBuilder` 的树形 trace 取代。

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

[Unreleased]: https://github.com/huleidada/matterloop/compare/v0.1.1...HEAD
[0.1.1]: https://github.com/huleidada/matterloop/releases/tag/v0.1.1
[0.1.0]: https://github.com/huleidada/matterloop/releases/tag/v0.1.0
