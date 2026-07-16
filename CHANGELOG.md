简体中文 | [English](CHANGELOG.en.md)

# Changelog

本文件记录 MatterLoop 的用户可感知变化。仓库中的 12 个发行包采用同一版本，因此每个版本条目
覆盖整套组件，而不是分别维护互相漂移的变更日志。

## [Unreleased]

### Added

- 全部公开 Markdown 新增英文镜像、双向语言切换和国际化契约测试。

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

[Unreleased]: https://github.com/huleidada/matterloop/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/huleidada/matterloop/releases/tag/v0.1.0
