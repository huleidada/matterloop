简体中文 | [English](https://github.com/huleidada/matterloop/blob/main/matterloop-observability/README.en.md)

# matterloop-observability

MatterLoop 的事件是业务事实，不是日志字符串。`matterloop-observability` 把 Core `LoopEvent`
接到日志、指标、树形 Trace 和评分，同时把日志后端与 OpenTelemetry 的进程级配置留给宿主应用。

```bash
pip install matterloop-observability
# 需要 OpenTelemetry 导出时（含 SDK 与 OTLP/HTTP Exporter）
pip install "matterloop-observability[otel]"
```

## 一次合理的装配

```python
import logging

from matterloop_observability import (
    CompositeEventPublisher,
    HandlerEventPublisher,
    MetricsHandler,
    PublisherFailureMode,
    Redactor,
    StructuredLoggingHandler,
)

redactor = Redactor(extra_fields=("tenant_secret", "session_credential"))
metrics = MetricsHandler()

events = CompositeEventPublisher(
    publishers=(
        HandlerEventPublisher(
            StructuredLoggingHandler(
                logger=logging.getLogger("app.matterloop.audit"),
                redactor=redactor,
            )
        ),
        HandlerEventPublisher(metrics),
    ),
    failure_mode=PublisherFailureMode.RAISE,
)
```

将 `events` 注入 `AgentLoop(events=...)`。处理器按顺序执行；同步处理器不创建后台队列，也不接管
Logger 或其关闭流程。唯一的例外是下文用于 Trace 导出的 `BatchingPipeline`：它持有后台守护线程，
由调用方在应用退出前 `shutdown()`。

## 失败策略要显式选择

`CompositeEventPublisher(publishers, failure_mode)` 支持两种策略：

- `LOG_AND_CONTINUE` 是默认值，适合可丢失的遥测。单个发布器失败会记录异常，并继续发布后续事件。
- `RAISE` 在第一个失败处停止，适合审计不可缺失的场景。代价是可观测性故障可能中断业务闭环。

如果要求“状态提交与审计记录同时成功”，顺序调用几个 Publisher 并不能提供事务保证。应使用
Outbox、持久化事件表或消息系统完成原子交接。

## 日志里有什么

`StructuredLoggingHandler(logger, redactor)` 输出单行 JSON，包含事件类型、`run_id`、Loop 状态、
发生时间、事件说明和请求 metadata。默认 Logger 名称是 `matterloop.events`；日志格式、轮转、
保留期和访问控制仍由应用配置。

`Redactor(extra_fields)` 会递归检查映射键，默认识别 `token`、`authorization`、`cookie`、
`api_key`、`password` 和 `secret`，也能命中 `access_token` 之类的前后缀名称。它不会扫描自由文本：
提示词、模型输出、URL 查询参数和异常堆栈里的秘密仍可能泄漏。不要把凭据放进 `goal`、`detail`
或任意字符串 metadata。

## 指标与 Trace

- `MetricsHandler` 保存当前进程内的事件计数，适合测试和轻量诊断。
- `OpenTelemetryMetricsHandler` 写入 `matterloop.loop.events`，只附带事件类型和 Loop 状态。
- `TracingHandler` 已废弃：它为每个事件创建孤立的短 Span，无法还原父子关系，请改用下文的
  `TraceBuilder`，它会在后续版本移除。

`OpenTelemetryMetricsHandler` 与 `TracingHandler` 只使用 API，宿主必须先配置 SDK、Exporter、采样
和资源属性，构造时缺少依赖会立即抛出 `RuntimeError`。`OtelExporter` 例外：它自带 SDK 与 OTLP/HTTP
Exporter（由 `[otel]` extra 提供），缺少依赖时构造抛出 `ImportError`。

## 树形 Trace 与评分

`TraceBuilder(pipeline)` 实现 Core `EventPublisher` 协议，把生命周期事件流重建为树形跨度结构：
根跨度覆盖整个运行，执行、验证、迭代快照和整体完成度验收各成跨度；验证跨度关闭时会把
`VerificationResult.score`（0–100）归一提取为 `Score`。已关闭的跨度和评分进入
`BatchingPipeline(exporter, flush_at, flush_interval)`，由后台守护线程聚批后交给 `SpanExporter`。

```python
from matterloop_observability import (
    BatchingPipeline,
    CompositeEventPublisher,
    JsonlExporter,
    PublisherFailureMode,
    TraceBuilder,
)

pipeline = BatchingPipeline(
    JsonlExporter("traces.jsonl"),
    flush_at=50,
    flush_interval=5.0,
)
trace_builder = TraceBuilder(pipeline)
events = CompositeEventPublisher(
    publishers=(audit_publisher, trace_builder),
    failure_mode=PublisherFailureMode.RAISE,
)
# 应用退出前：pipeline.shutdown()
```

`JsonlExporter(path)` 每行追加一个带 `type` 字段的 JSON 记录，零额外依赖。`OtelExporter(endpoint)`
按原父子关系和起止时间把跨度重建到 OTLP/HTTP 后端，评分导出为同一 trace 下名为 `score:<name>`
的瞬时子跨度。实际 OTel trace/span ID 由 SDK 生成，MatterLoop 的 `run_id`、`span_id` 和父标识分别
保存在 `matterloop.trace_id`、`matterloop.span_id`、`matterloop.parent_span_id` 属性中。流水线队列有界
（默认 10000），满时丢新并告警；OTel 需等待根跨度到达以建立公开 API 的父子 context，单运行暂存同样
默认最多 10000 条，超过后丢新并告警。导出失败重试一次后丢弃，任何异常都不会抛回 Loop 主流程。

`SpanRecord` 是不可变跨度记录：`trace_id`（即产生跨度的 `run_id`）、`span_id`、`parent_span_id`、
`name`、`observation_type`、`started_at`、`ended_at`、`attributes`、`level` 和 `status_message`。
`Score` 是不可变评分：`name`、`value`（NUMERIC 归一到 0–1）、`data_type`、`source`、`run_id`、
`step_id`、`comment`、`evidence` 和 `timestamp`。`score_from_verification` 完成验证结论到 NUMERIC
评分的映射；`score_from_review` 接受具备 `score`/`summary`/`evidence` 属性的鸭子类型审查结论，
不要求安装 agents 组件。

## 模型调用跨度

```python
from matterloop_observability import wrap_model_client

client = wrap_model_client(model_client, trace_builder)
```

`TracedModelClient(client, trace_builder, pipeline)` 可包装任意 `ModelClient`：请求 metadata 含
`run_id` 时记录一个 generation 跨度，内容包含脱敏后的输入消息、采样参数、输出文本和六项 Token
用量，父跨度由 `trace_builder` 按 `run_id`/`step_id` 解析，解析不到时挂到运行根跨度；metadata
缺少 `run_id` 时直接透传，观测永远不会阻断调用。模型异常会记录 ERROR 跨度并原样继续抛出。
agents 组件的 Planner、Worker、Verifier 和 Reviewer 已在请求 metadata 中写入 `run_id`、`step_id`
和 `agent`，包装注册进 `ModelRegistry` 的客户端即可自动获得模型跨度；production preset 可通过
`trace_exporter` 参数一键完成这套装配，见 [matterloop-presets](../matterloop-presets/README.md)。

## 扩展方式

同步或异步 callable 可用 `HandlerEventPublisher(handler)` 接入。跨度与评分的批量、重试和背压已由
`BatchingPipeline` 提供；需要自定义事件去向时，直接实现 Core `EventPublisher.publish(event)`，
在实现内部管理有界队列和关闭流程。

本包当前面向 Core `LoopEvent`。TeamLoop 事件的数据结构不同，需要单独适配，不能把团队事件发布器
直接塞给这些处理器。生产拓扑和关闭顺序见[企业集成指南](../docs/enterprise-integration.md)。
