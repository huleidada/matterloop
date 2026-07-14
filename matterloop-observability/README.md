# matterloop-observability

> 跨模块生产拓扑、资源所有权与上线检查见[企业集成指南](../docs/enterprise-integration.md)。

`matterloop-observability` 把 Core `LoopEvent` 接入结构化日志、指标和追踪。标准库日志与进程内
指标不需要第三方依赖；OpenTelemetry 处理器通过 `matterloop-observability[otel]` 安装 API。
本包不创建日志 Handler、OpenTelemetry SDK、Exporter 或采样器，这些进程级资源由宿主应用配置。

## 公共组件

| 组件 | 构造参数与真实默认 | 职责 |
| --- | --- | --- |
| `Redactor` | `extra_fields=()` | 按字段名递归生成脱敏副本 |
| `StructuredLoggingHandler` | `logger=None`、`redactor=None` | 默认向 `logging.getLogger("matterloop.events")` 写 INFO 单行 JSON |
| `MetricsHandler` | 无参数 | 按事件类型保存进程内计数 |
| `OpenTelemetryMetricsHandler` | `meter_name="matterloop"` | 向计数器 `matterloop.loop.events` 加一 |
| `TracingHandler` | `tracer_name="matterloop"` | 为每个事件创建一个短 Span |
| `HandlerEventPublisher` | `handler` 必填 | 把同步或异步事件处理器适配为 Core `EventPublisher` |
| `CompositeEventPublisher` | `publishers` 必填；`failure_mode=LOG_AND_CONTINUE` | 按传入顺序串行调用多个发布器 |

`PublisherFailureMode` 只有两种稳定值：`RAISE` 和 `LOG_AND_CONTINUE`。后者是代码默认值，适合
非关键遥测；审计合规链路通常应显式使用前者。

## 企业装配

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

audit_logger = logging.getLogger("application.matterloop.audit")
redactor = Redactor(extra_fields=("tenant_secret", "customer_token"))
metrics = MetricsHandler()

publisher = CompositeEventPublisher(
    (
        HandlerEventPublisher(StructuredLoggingHandler(audit_logger, redactor)),
        HandlerEventPublisher(metrics),
    ),
    failure_mode=PublisherFailureMode.RAISE,
)

# 把 publisher 注入 AgentLoop(events=...) 或 production preset 的 audit_publisher。
```

处理器按元组顺序执行；同步处理器直接调用，返回 awaitable 时会等待完成。当前实现不并行发送、
不排队、不重试，也不拥有外部日志或遥测资源的关闭职责。高吞吐应用应在自定义 Publisher 后面接
有界队列，并自行定义背压、落盘和关闭语义。

## 结构化日志字段

`StructuredLoggingHandler` 输出以下字段，键按字典序写为一行 JSON：

| 字段 | 来源 |
| --- | --- |
| `event` | `LoopEvent.event_type.value` |
| `run_id` | 当前上下文运行标识 |
| `status` | 当前 Loop 状态 |
| `occurred_at` | 带时区的 ISO 时间 |
| `detail` | 生命周期事件说明 |
| `metadata` | `LoopRequest.metadata` 的浅层副本，再递归脱敏 |

默认 logger 只有名称和日志级别语义；是否真正输出、输出格式、轮转、保留和访问控制取决于宿主的
`logging` 配置。metadata 中的值必须能被标准库 `json.dumps()` 序列化，否则处理器会抛出异常，
再由外层发布器的失败模式决定是否中断 Loop。

## 脱敏边界

默认敏感字段为 `token`、`authorization`、`cookie`、`api_key`、`password` 和 `secret`。
字段匹配不区分大小写，并把 `-`、`.` 规范为 `_`；完整名称以及前后缀形式都会命中，例如
`access_token`、`set-cookie` 和 `customer.secret`。Mapping 会递归复制，序列会转换为列表，原对象
不会被修改。

这是一道字段级防线，不是数据防泄漏系统：

- 普通字符串中的密钥、提示词、模型输出和 URL 查询参数不会被扫描；
- 自定义对象不会被展开；循环引用也不受支持；
- `CompositeEventPublisher` 在 `LOG_AND_CONTINUE` 下记录的异常栈不经过 `Redactor`；
- 团队事件、第三方日志和供应商 SDK 日志不会自动使用本处理器。

生产环境应在进入 MatterLoop 前禁止把秘密放入 goal、detail 或自由文本 metadata，并在日志后端
继续实施字段策略、访问控制和保留期限。

## 指标与追踪

`MetricsHandler.count(event_name)` 返回当前进程自启动以来的计数；它不持久化、不跨进程汇总，
也不提供标签查询。`OpenTelemetryMetricsHandler` 只写入 `event.type` 与 `loop.status` 两个低基数
属性。`TracingHandler` 为每个事件创建短 Span，并设置 `loop.run_id` 与 `loop.status`；它不会创建
覆盖完整 Loop 的父 Span。

如果 `opentelemetry.metrics` 或 `opentelemetry.trace` 无法导入，构造处理器会立即抛出
`RuntimeError`。宿主必须先配置 MeterProvider、TracerProvider、Exporter、采样和资源属性；仅安装
extra 不会自动导出数据。评估后端计费与基数策略时，应特别处理 `loop.run_id` 这一高基数属性。

## 失败模式与生产建议

- `LOG_AND_CONTINUE`：捕获每个子发布器的异常、用本包 logger 记录异常栈，然后继续下一个；业务
  可能成功而审计缺失。
- `RAISE`：原样抛出第一个异常，后续发布器不再执行；production preset 会把审计 Publisher 包装
  成这一模式。
- 本包没有内置重试或事务；要求“业务状态与审计同时提交”时，应由持久化事件表、Outbox 或消息
  系统实现，而不是依赖多个顺序 Publisher。
- 进程关闭前应先停止接收新运行，再排空自定义遥测队列，最后关闭宿主的 Exporter/Provider。
- `LocalTeamEventPublisher` 发布的是 TeamLoop 事件，不是 Core `LoopEvent`，需要独立的团队事件
  适配器，不能直接传给这些 Core 处理器。
