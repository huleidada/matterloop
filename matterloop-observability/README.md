# matterloop-observability

MatterLoop 的事件是业务事实，不是日志字符串。`matterloop-observability` 把 Core `LoopEvent`
接到日志、指标和 Trace，同时把日志后端与 OpenTelemetry 的进程级配置留给宿主应用。

```bash
pip install matterloop-observability
# 需要 OpenTelemetry 时
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

将 `events` 注入 `AgentLoop(events=...)`。处理器按顺序执行；本包不创建后台队列，也不接管 Logger、
Exporter、Provider 或它们的关闭流程。

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
- `TracingHandler` 为每个事件创建短 Span，不会自动构造覆盖整个 Loop 的父 Span。

OpenTelemetry 组件只使用 API。宿主必须先配置 SDK、Exporter、采样和资源属性；安装 extra 不等于
数据会被导出。构造时缺少 OpenTelemetry 依赖会立即抛出 `RuntimeError`。

## 扩展方式

同步或异步 callable 可用 `HandlerEventPublisher(handler)` 接入。需要批量、重试或背压时，直接实现
Core `EventPublisher.publish(event)`，在实现内部管理有界队列和关闭流程。

本包当前面向 Core `LoopEvent`。TeamLoop 事件的数据结构不同，需要单独适配，不能把团队事件发布器
直接塞给这些处理器。生产拓扑和关闭顺序见[企业集成指南](../docs/enterprise-integration.md)。
