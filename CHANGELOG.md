简体中文 | [English](CHANGELOG.en.md)

# Changelog

本文件记录 MatterLoop 的用户可感知变化。仓库中的 12 个发行包采用同一版本，因此每个版本条目
覆盖整套组件，而不是分别维护互相漂移的变更日志。

## [Unreleased]

## [0.2.3] - 2026-08-03

### Fixed

- Runtime 语义压缩不再要求模型回传内部 Context 条目 ID；来源 ID 改由运行时根据原始输入确定性写入，
  避免模型漏写或改写 ID 时在硬阈值处触发 `context compaction failed`。

## [0.2.2] - 2026-07-29

### Added

- Agents 新增可组合的意图识别架构：通过强类型候选、优先级、置信度与冲突阈值解析多意图输入，
  并在信号不足或候选冲突时显式返回不确定结果，避免错误路由。
- Agents 新增可插拔评分架构：支持逐条件证据、权重、最低分、必选条件和归一化总分，使不同任务
  能选择合适的评分策略，而不把特定业务验收字段写死在通用运行时中。

### Changed

- `CriteriaVerifier` 现在按条件逐项生成结构化评估，并通过注入的评分策略计算最终结果；
  验证提示词和运行时协议字段统一使用英文，中文保留在代码注释与说明文档中。

## [0.2.1] - 2026-07-29

### Added

- Observability 新增 `OpenTelemetryToolMiddleware`，自动记录 Tool、Skill 和 MCP 调用；默认只记录
  载荷大小、SHA-256 和白名单语义属性。只有显式设置 `capture_tool_payloads=True` 才按原文采集有界
  载荷（默认 4096 字节，可通过 `capture_max_body_bytes` 覆盖）。production preset 支持显式
  `tools`/`tool_authorizer` 注入。
- Observability 新增 Team/子 Agent 实时 OTel 拓扑；Team 快照持久化 W3C carrier，暂停、阻塞和
  跨进程恢复仍保持 `matterloop.team -> matterloop.team.agent -> matterloop.run` 父子关系。

### Changed

- Checkpoint schema v2 同时包含 `propagation_context` 与 `external_state_refs`，并兼容读取旧的
  无版本 v1。
- Trace Span 名称统一为固定的 `matterloop.*` 语义，动态 agent/executor 信息改为属性；
  `OtelExporter.aclose()` 会 force-flush，并只 shutdown 自己创建的 Provider。
  这是观测 schema 的不兼容变更；现有 dashboard、告警和查询必须迁移到新的固定 Span 名称。
- Runtime 关闭会先 drain 在途 Loop 和工具调用，再关闭工具、模型与 exporter，避免已结束 Provider
  漏掉晚结束的 `matterloop.tool` Span。

## [0.2.0] - 2026-07-28

### Added

- Runtime 内置 Context Lifecycle Engine：覆盖 Token 监测、工具结果外置、原生/语义压缩、不可变快照、
  Redis CAS、长期任务恢复和可选记忆抽取，不新增发行包。
- Models 新增规范化上下文输入、模型上下文作用域、精确 Token 计数和原生压缩能力协议；Agent 与 Team
  全部模型调用接入稳定作用域。
- Observability 新增树形 tracing 与评分：`TraceBuilder` 把生命周期事件流重建为跨度树，
  `BatchingPipeline` 聚批导出 `SpanRecord` 与 `Score`，提供 JSONL 与 OTLP/HTTP 两种导出器；
  `TracedModelClient` 包装模型客户端自动记录 generation 跨度。
- production preset 新增可选 `trace_exporter` 参数，一键把 TraceBuilder 挂入审计事件管线并包装模型
  客户端，导出流水线随 runtime 关闭自动排空。
- Core 新增公开生命周期事件 `LoopEventType.COMPLETION_EVALUATION_COMPLETED`，在整体验收决策
  （通过/重规划/请求人工）产生后发出，订阅者可精确获知评估结束时机。
- Observability 新增实时 OTel 追踪：`OpenTelemetryTracePublisher` 在 Loop 执行期间维护真实
  Span 上下文，`OpenTelemetryModelClient` 嵌套记录 generation，数据库/HTTP 自动
  instrumentation 可进入同一条 Trace；阻塞/暂停时将 W3C `traceparent`/`tracestate` 与 checkpoint
  同次 CAS 保存，恢复后创建真实子 Span，`run_id` 仅用于业务关联。
- Core 新增 `CheckpointPreparer` 协议与 `LoopContext.propagation_context` 字段：事件发布器可在
  checkpoint CAS 保存前写入可持久化的关联信息（如 W3C 传播上下文），`CompositeEventPublisher`
  会转发该钩子。

### Changed

- Checkpoint 升级到 schema v2，新增 `external_state_refs` 并兼容读取旧的无版本 v1。

### Deprecated

- `TracingHandler` 标记废弃，孤立短跨度由 `TraceBuilder` 的树形 trace 取代。

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

[Unreleased]: https://github.com/huleidada/matterloop/compare/v0.2.3...HEAD
[0.2.3]: https://github.com/huleidada/matterloop/releases/tag/v0.2.3
[0.2.2]: https://github.com/huleidada/matterloop/releases/tag/v0.2.2
[0.2.1]: https://github.com/huleidada/matterloop/releases/tag/v0.2.1
[0.2.0]: https://github.com/huleidada/matterloop/releases/tag/v0.2.0
[0.1.2]: https://github.com/huleidada/matterloop/releases/tag/v0.1.2
[0.1.1]: https://github.com/huleidada/matterloop/releases/tag/v0.1.1
[0.1.0]: https://github.com/huleidada/matterloop/releases/tag/v0.1.0
