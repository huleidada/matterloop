简体中文 | [English](https://github.com/huleidada/matterloop/blob/main/matterloop-presets/README.en.md)

# matterloop-presets

Presets 是一组有明确取舍的组合根。它们帮你把模型、Agent、工具、策略、检查点和 Runtime 接起来，
但不会隐藏这些组件，也不会把开发配置伪装成生产方案。

```bash
pip install matterloop-presets
```

## 选哪个

| 场景 | Builder | 默认能力 | 需要你补充 |
| --- | --- | --- | --- |
| 纯模型任务、测试 | `build_minimal_runtime` | 无工具，内存 checkpoint | 一个 `ModelClient` |
| 受控代码修改 | `build_coding_runtime` | 只读文件；审批后可写文件、运行白名单命令 | 模型、workspace、审批策略 |
| 有来源要求的研究 | `build_research_runtime` | 只读文件、HTTPS GET、引用门槛 | 模型、workspace、host allowlist |
| API 与 Worker 分离 | `build_production_runtime` | 队列门面、外部 checkpoint、强失败审计 | 全部生产基础设施 |

对应的同步入口是 `build_minimal_local_runtime`、`build_coding_local_runtime`、
`build_research_local_runtime` 和 `build_production_local_runtime`。同步版本使用专用事件循环线程，
不是另一套编排实现。

四个异步 Builder 的关键入参分别是：minimal 接收 `model` 和可选 `config`；coding 接收 `model`、
`workspace`、可选 `config` 与 `approval_gate`；research 接收 `model`、`workspace` 和必填 `config`；
production 接收 `model`、可选 `config`、四项必需基础设施，以及可选 `event_reader` 与
`approval_gate`。

## 先跑起来

```python
from matterloop_core import LoopRequest
from matterloop_presets import build_minimal_runtime

async with build_minimal_runtime(model=model_client) as runtime:
    result = await runtime.run(LoopRequest(goal="整理发布检查项"))
```

模型客户端由应用构造。Preset 不读 `.env`，也不创建 OpenAI、DeepSeek、千问、智谱或 MiniMax SDK。

## Coding：权限按执行器分层

```python
from matterloop_presets import CodingPresetConfig, build_coding_runtime

runtime = build_coding_runtime(
    model=model_client,
    workspace="/srv/workspaces/job-42",
    config=CodingPresetConfig(
        allowed_commands=frozenset({"pytest", "ruff"}),
        shell_environment={"PATH": "/opt/matterloop/bin:/usr/bin"},
    ),
    approval_gate=approval_gate,
)
```

`default` 执行器只能读文件；`privileged_executor` 额外拥有写文件和受限 Shell。指向高权限执行器的
步骤会被强制标记为需要审批。没有传 `approval_gate` 时，审批会延期并让 Loop 暂停，而不是默认
放行。Shell 不经过命令解释器，也不继承宿主环境；这仍然不是恶意代码隔离。

## Research：限制入口，不替来源背书

```python
from matterloop_presets import ResearchPresetConfig, build_research_runtime

runtime = build_research_runtime(
    model=model_client,
    workspace="/srv/reference-data",
    config=ResearchPresetConfig(allowed_hosts=frozenset({"docs.example.com"})),
)
```

HTTP 固定为 HTTPS `GET`、精确 host allowlist 且不跟随重定向。`require_citation=True` 只检查结果中
是否存在 URL 或 artifact 引用，不证明来源真实、最新或可信。

## Production：只装配，不替你跑 Worker

```python
from matterloop_presets import build_production_runtime

runtime = build_production_runtime(
    model=model_client,
    config=production_config,
    queue_backend=queue_backend,
    run_repository=run_repository,
    checkpoint_store=checkpoint_store,
    audit_publisher=audit_publisher,
    event_reader=event_reader,
    approval_gate=approval_gate,
    trace_exporter=JsonlExporter("traces.jsonl"),
)
```

`queue_backend`、`run_repository`、`checkpoint_store` 和 `audit_publisher` 缺一即抛
`PresetConfigurationError`，不会回退到内存实现。返回的 `ProductionRuntime` 包含控制面的
`queue_runtime` 和执行面的 `worker_runtime`；租约、ack、续租、死信和 Worker 循环仍由部署方负责。

`trace_exporter` 是可选的：传入普通 `SpanExporter`（如 `JsonlExporter`）时，preset 会把
`TraceBuilder` 挂入审计事件管线、把模型客户端包装为 `TracedModelClient`，并在
`ProductionRuntime.aclose()` 时自动排空导出流水线。传入共享 `TracerProvider` 的 `OtelExporter` 时，
preset 改为实时 OTel Context：`matterloop.run`、各执行阶段、generation 以及 SQLAlchemy/HTTP 等自动
instrumentation 的 Span 处在同一条 Trace。Provider 的创建、全局注册和关闭仍由应用负责；完整的数据库
配置见 [matterloop-observability](../matterloop-observability/README.md#生产环境与数据库共用一条实时-otel-trace)。
阻塞/暂停会把 W3C `traceparent`/`tracestate` 与 Loop checkpoint 使用同一次 CAS 持久化，恢复会创建真实
子 Span，不依赖 `run_id` 派生或合成父节点；W3C baggage 不会写入 checkpoint。
缺省不创建任何 tracing 资源，事件管线行为与之前完全一致。

## 配置速查

配置均为 frozen dataclass：

- `AgentPresetConfig(model_name, max_plan_steps, max_tool_rounds, pass_score, max_identical_feedback, retry)`：
  默认分别为 `"default"`、`20`、`8`、`80`、`2` 和默认重试配置。
- `MinimalPresetConfig` 与 `ProductionPresetConfig` 当前不增加字段。
- `CodingPresetConfig` 增加 `privileged_executor`、`allowed_commands`、`shell_environment`、
  `max_read_bytes`、`max_write_bytes`、`max_shell_timeout_seconds` 和 `max_shell_output_bytes`。默认高权限
  执行器为 `"coding"`，
  命令集为 `pytest/ruff`，文件与输出上限为 1,000,000 字节，命令最长 60 秒。
- `ResearchPresetConfig` 增加 `allowed_hosts`、`max_read_bytes`、`max_response_bytes`、
  `max_http_timeout_seconds` 和 `require_citation`。host 集合实际必填；默认单文件 1,000,000 字节、响应
  2,000,000 字节、请求 20 秒并要求引用。

`max_plan_steps` 和 `max_tool_rounds` 不是总预算。cycle、attempt、步骤数和活跃超时来自
`LoopRequest.limits`；Token、费用和并发额度需要在 `matterloop-policies` 中单独装配。

## 生命周期与边界

异步 Runtime 使用 `async with`/`aclose()`，同步 Runtime 使用 `with`/`close()`。Preset 会关闭自己
登记的模型适配器和工具注册表，但不会关闭外部队列、仓储、Redis 或审计后端。热替换后加入的新
组件也需要调用方管理退役与关闭。

需要多模型角色、持久预算、领域验证器或自定义工具权限时，应直接装配基础包；继续叠加 Preset
配置通常会让所有权更难理解。完整组合方式见[企业集成指南](../docs/enterprise-integration.md)。
