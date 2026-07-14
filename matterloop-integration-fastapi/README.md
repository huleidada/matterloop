# matterloop-integration-fastapi

> 跨模块生产拓扑、资源所有权与上线检查见[企业集成指南](../docs/enterprise-integration.md)。

`matterloop-integration-fastapi` 把 MatterLoop 异步运行时暴露为可挂载的 FastAPI 路由。
集成层只负责输入校验、鉴权依赖、DTO 转换和稳定异常映射，不包含 Loop 编排、队列处理或
运行持久化逻辑。

## 安装

```bash
pip install matterloop-integration-fastapi
```

## 使用

```python
from fastapi import FastAPI, Header, HTTPException
from matterloop_integration_fastapi import create_router


async def authenticate(x_api_key: str | None = Header(default=None)) -> str:
    if x_api_key != "expected-secret":
        raise HTTPException(status_code=401, detail="unauthorized")
    return x_api_key


app = FastAPI()
app.include_router(create_router(runtime, authenticate))
```

鉴权依赖会应用于路由器内的全部端点。默认前缀为 `/loops`，也可以通过 `prefix` 参数修改。

## 路由

| 方法 | 路径 | 能力 |
|---|---|---|
| `POST` | `/loops/create` | 创建或提交运行 |
| `GET` | `/loops/list` | 分页列出队列运行 |
| `GET` | `/loops/{run_id}` | 查询队列运行 |
| `POST` | `/loops/{run_id}/cancel` | 请求协作式取消 |
| `POST` | `/loops/{run_id}/resume` | 精确继续或重新规划 |
| `GET` | `/loops/{run_id}/events/list` | 分页读取审计事件 |

`create_router` 在创建时一次性识别两种结构协议：

- `QueueRuntimeProtocol` 对应 `matterloop_runtime.QueueRuntime`，支持全部路由。
- `DirectRuntimeProtocol` 对应 `matterloop_runtime.AsyncRuntime`，支持 create、cancel 和 resume。
  因为直接运行时没有运行仓储，list、get 和 events 会明确返回 HTTP 501。

## HTTP 错误语义

- 请求字段不合法：`422`
- 运行不存在：`404`
- 运行状态冲突或重复标识：`409`
- 运行时已关闭：`503`
- 当前运行时没有查询仓储：`501`

路由异常响应使用固定文案，不直接返回被捕获异常的文本。需要注意，正常的 `RunResponse.error`
来自持久化运行记录或 `LoopResult`，当前会原样返回；应用必须在写入记录前完成错误脱敏，或在
对外网关增加响应过滤。

## 稳定公共入口

包级 `matterloop_integration_fastapi.__all__` 导出以下 API：

| 分组 | 公共 API |
|---|---|
| 路由工厂 | `create_router` |
| 运行时结构协议 | `RuntimeProtocol`、`DirectRuntimeProtocol`、`QueueRuntimeProtocol` |
| 请求 DTO | `CreateLoopRequest`、`LoopLimitsRequest`、`ResumeLoopRequest` |
| 响应 DTO | `RunResponse`、`ResumeResponse`、`CancelResponse`、`EventListResponse` |
| 审计 DTO | `PlanStepResponse`、`ArtifactResponse`、`ExecutionResponse`、`VerificationResponse`、`IterationResponse` |

所有 HTTP DTO 都继承同一严格 Pydantic 配置，未知字段会被拒绝，不能依赖服务端静默忽略
拼写错误或未来字段。

## 路由工厂参数

| 参数 | 类型 | 必填 | 默认 | 业务含义 | 校验与生命周期 |
|---|---|---:|---|---|---|
| `runtime` | `RuntimeProtocol` | 是 | 无 | 直接运行或队列运行门面 | 构造路由时执行一次结构协议识别；同时满足两种协议时优先按 Queue Runtime 处理 |
| `auth_dependency` | `Callable[..., object]` | 是 | 无 | 应用于全部路由的 FastAPI 依赖 | 必须可调用；返回值不由本包消费，鉴权失败应由依赖抛出 HTTP 异常 |
| `prefix` | `str` | 否 | `"/loops"` | 全部端点前缀 | 去除外围空白和尾部 `/`；必须以 `/` 开头、不能是根路径或包含路径参数 `{}` |

`create_router` 只创建 `APIRouter`，不会启动或关闭 Runtime、连接池和后台 Worker。应用必须在
FastAPI lifespan 中构造共享依赖并在退出时调用相应的 `aclose()`。

## 请求字段

### `LoopLimitsRequest`

| 字段 | 类型 | 必填 | 默认 | 业务含义 | 校验不变量 | 安全与持久化 |
|---|---|---:|---|---|---|---|
| `max_cycles` | `int` | 否 | `5` | 最大规划轮次 | `>= 1` | 应按租户设置更严格上限 |
| `max_attempts` | `int` | 否 | `20` | Executor 最大总调用次数 | `>= 1` | 不代表模型、工具或费用预算 |
| `max_steps_per_plan` | `int` | 否 | `20` | 单个计划最大步骤数 | `>= 1` | 防止单次计划无限膨胀 |
| `timeout_seconds` | `float \| None` | 否 | `None` | Core 活跃执行超时 | 非空时必须 `> 0` | `None` 表示本层不设时限；网关仍应配置请求超时 |

### `CreateLoopRequest`

| 字段 | 类型 | 必填 | 默认 | 业务含义 | 校验不变量 | 安全与持久化 |
|---|---|---:|---|---|---|---|
| `goal` | `str` | 是 | 无 | Loop 总目标 | 去除首尾空白后至少 1 个字符 | 当前无最大长度；应用应限制请求体并做内容分级 |
| `acceptance_criteria` | `tuple[str, ...]` | 否 | `()` | 整体目标验收条件 | 每项去除空白后非空 | 会进入模型和检查点，可能包含敏感业务信息 |
| `limits` | `LoopLimitsRequest` | 否 | 默认对象 | 运行边界 | 见上表 | Queue 模式会随 `LoopRequest` 持久化 |
| `metadata` | `dict[str, JsonValue]` | 否 | `{}` | 关联 ID、租户和审计扩展值 | 只允许 JSON 值，拒绝未知顶层字段 | 不会自动脱敏、加密或校验租户归属 |
| `run_id` | `str \| None` | 否 | `None` | 调用方提供的幂等运行标识 | 1–128 字符；首字符为字母或数字；仅允许字母、数字、`.`、`_`、`:`、`-` | Queue 仓储会拒绝重复标识；不要编码密钥或个人数据 |

### `ResumeLoopRequest`

| 字段 | 类型 | 必填 | 默认 | 业务含义 | 校验不变量 | 安全与持久化 |
|---|---|---:|---|---|---|---|
| `mode` | `ResumeMode` | 否 | `continue` | 精确继续或强制重新规划 | 仅接受 `continue`、`replan` | `continue` 不会自动降级为重新规划 |

## 响应字段

### `RunResponse`

| 字段 | 类型 | 必填 | 默认 | 业务含义 | 来源与安全边界 |
|---|---|---:|---|---|---|
| `run_id` | `str` | 是 | 无 | 运行标识 | 来自 Core 结果或 Queue 记录 |
| `status` | `str` | 是 | 无 | Core/Queue 状态字符串 | 两套状态集合不同，客户端应按字符串兼容处理 |
| `output` | `str` | 否 | `""` | 最后一条执行记录的输出 | 可能含模型或工具敏感数据，不做脱敏 |
| `cycles` | `int` | 否 | `0` | 已消耗规划轮次 | Queue 尚无结果时为 0 |
| `total_attempts` | `int` | 否 | `0` | Executor 总尝试次数 | Queue 尚无结果时为 0 |
| `completed_steps` | `int` | 否 | `0` | 已形成执行和验证记录的步骤数 | 包括验证未通过的步骤记录 |
| `records` | `tuple[IterationResponse, ...]` | 否 | `()` | 步骤级审计轨迹 | 不含执行、制品 metadata，但包含正文和证据 |
| `stop_reason` | `str \| None` | 否 | `None` | Core 结构化停止原因 | Queue 尚无结果时为空 |
| `error` | `str` | 否 | `""` | 持久化错误摘要 | 当前原样返回，必须由上游保证不含凭据、路径或供应商正文 |
| `goal` | `str \| None` | 否 | `None` | Queue 记录中的原始目标 | 直接运行结果不会填充；可能敏感 |
| `version` | `int \| None` | 否 | `None` | Queue 记录 CAS 版本 | 直接运行结果为空 |
| `created_at` | `datetime \| None` | 否 | `None` | Queue 记录创建时间 | 直接运行结果为空；序列化为带时区时间 |
| `updated_at` | `datetime \| None` | 否 | `None` | Queue 记录最后更新时间 | 直接运行结果为空 |

### 步骤审计 DTO

| DTO.字段 | 类型 | 必填 | 默认 | 业务含义 | 安全与持久化 |
|---|---|---:|---|---|---|
| `PlanStepResponse.step_id` | `str` | 是 | 无 | 步骤稳定标识 | 不应作为独立授权凭据 |
| `PlanStepResponse.description` | `str` | 是 | 无 | 步骤说明 | 可能含用户输入或模型内容 |
| `PlanStepResponse.executor` | `str` | 是 | 无 | 执行器注册名 | 会暴露内部能力命名，公网 API 可按需过滤 |
| `PlanStepResponse.acceptance_criteria` | `tuple[str, ...]` | 是 | 无 | 步骤验收条件 | 可能敏感 |
| `PlanStepResponse.requires_approval` | `bool` | 是 | 无 | 是否需审批 | 仅表示计划声明，不表示已经批准 |
| `ArtifactResponse.name` | `str` | 是 | 无 | 制品名称 | 不返回 Core `ArtifactRef.metadata` |
| `ArtifactResponse.uri` | `str` | 是 | 无 | 制品位置 | 不会签名或隐藏 URI；不得返回内部凭据化 URL |
| `ArtifactResponse.media_type` | `str \| None` | 是 | 无 | IANA 媒体类型 | 不验证内容本身 |
| `ExecutionResponse.output` | `str` | 是 | 无 | Executor 输出 | 不返回 `ExecutionResult.metadata`，正文仍可能敏感 |
| `ExecutionResponse.artifacts` | `tuple[ArtifactResponse, ...]` | 是 | 无 | 外部制品引用 | API 不检查 URI 可访问性 |
| `VerificationResponse.passed` | `bool` | 是 | 无 | 验证结论 | 不是整体目标完成结论 |
| `VerificationResponse.feedback` | `str` | 是 | 无 | 反馈 | 可能进入下一轮规划 |
| `VerificationResponse.score` | `float \| None` | 是 | 无 | 0–100 分或无评分 | 来自 Core 已校验 DTO |
| `VerificationResponse.evidence` | `tuple[str, ...]` | 是 | 无 | 支持结论的证据 | 不做引用真实性验证 |
| `VerificationResponse.failed_criteria` | `tuple[str, ...]` | 是 | 无 | 未满足条件 | passed 为真时 Core 要求为空 |
| `IterationResponse.cycle` | `int` | 是 | 无 | 规划轮次 | 从 1 开始 |
| `IterationResponse.step_index` | `int` | 是 | 无 | 计划内零基索引 | 从 0 开始 |
| `IterationResponse.attempt` | `int` | 是 | 无 | 当前步骤内部尝试序号 | 从 1 开始 |
| `IterationResponse.step` | `PlanStepResponse` | 是 | 无 | 步骤快照 | 不包含任意 metadata |
| `IterationResponse.execution` | `ExecutionResponse` | 是 | 无 | 执行快照 | 见上表 |
| `IterationResponse.verification` | `VerificationResponse` | 是 | 无 | 验证快照 | 见上表 |

### 操作响应

| DTO.字段 | 类型 | 必填 | 默认 | 业务含义 | 安全与持久化 |
|---|---|---:|---|---|---|
| `ResumeResponse.accepted` | `bool` | 是 | 无 | 恢复是否被接受 | Direct Runtime 成功返回时固定为 `True` |
| `ResumeResponse.run` | `RunResponse` | 是 | 无 | 最新运行视图 | Queue 模式通常仍是 `queued` |
| `CancelResponse.run_id` | `str` | 是 | 无 | 取消目标 | 不证明调用方拥有该运行 |
| `CancelResponse.accepted` | `bool` | 是 | 无 | 取消请求是否被接收 | 不表示执行已停止 |
| `EventListResponse.items` | `tuple[dict[str, object], ...]` | 是 | 无 | 事件读取器返回的分页事件 | 仅经 `jsonable_encoder` 转换，不执行字段级脱敏 |

## 结构协议方法

| 协议 | 必需方法 | 真实语义 |
|---|---|---|
| `DirectRuntimeProtocol` | `run(request, run_id=None)`、`resume(run_id, mode=continue)`、`cancel(run_id)` | create/resume 会在 HTTP 请求内等待 Runtime 返回；没有持久查询目录 |
| `QueueRuntimeProtocol` | `submit`、`get`、`list(limit=100, offset=0)`、`cancel`、`resume`、`list_events(after=None, limit=100)` | create/resume 只提交命令；查询依赖共享 `RunRepository` |

协议使用 `runtime_checkable` 做结构识别，不要求继承。传入对象的方法仍必须真正返回 awaitable；
只有同名同步方法的对象可能通过浅层结构检查，却会在请求执行时报错。

## 端点行为与分页边界

| 端点 | Queue Runtime | Direct Runtime | 参数边界 |
|---|---|---|---|
| `POST /create` | 创建记录并返回通常为 `queued` 的视图 | 阻塞等待 Loop 完成、暂停或阻塞 | body 严格校验；成功为 201 |
| `GET /list` | 按仓储顺序返回记录 | 501 | `limit=100`，1–500；`offset=0`，不得为负 |
| `GET /{run_id}` | 返回记录或 404 | 501 | run_id 1–128 且使用受限字符集 |
| `POST /{run_id}/cancel` | 不存在为 404；返回协作式接受结果 | 无目录，直接转发取消 | 不等待实际停止 |
| `POST /{run_id}/resume` | 重新排队并返回最新记录 | 在请求内执行恢复 | `mode` 默认 `continue` |
| `GET /{run_id}/events/list` | 读取事件；Runtime 未配置 reader 时可能返回空列表 | 501 | `after` 最长 256；`limit=100`，1–500 |

## HTTP 错误映射

| 条件 | 状态码 | 固定响应语义 |
|---|---:|---|
| Pydantic body/path/query 校验失败 | 422 | FastAPI 标准校验详情 |
| Runtime 或领域层 `ValueError` | 400 | `运行请求参数无效` |
| 其他可处理的 `MatterLoopError` | 400 | `MatterLoop 请求无法处理` |
| Core/Runtime 找不到运行 | 404 | `运行不存在` |
| 不可恢复、非法状态、重复 run_id | 409 | `运行状态与当前操作冲突` |
| Runtime 已关闭 | 503 | `运行时当前不可用` |
| Direct Runtime 调用查询或事件目录 | 501 | `当前运行时未配置运行仓储` |
| Queue create 后记录无法读取 | 500 | `运行记录创建后不可读取` |
| 未分类异常 | 500 | 交给 FastAPI/ASGI 异常处理器 |

## 企业安全与生命周期

- `auth_dependency` 只提供挂载点；本包不提供认证、租户授权、run_id 所有权检查、速率限制、
  CORS、CSRF、审计策略或请求体大小限制。
- 对每个读写端点都必须校验“当前身份是否有权访问该 run_id”，不能只验证凭据有效。
- Queue Runtime 更适合生产 HTTP 服务。Direct Runtime 会让长时间 Loop 占用请求连接，且无法
  提供 list/get/events 查询语义。
- Runtime、数据库、队列和模型客户端应在应用 lifespan 中创建一次并安全关闭；不要每个请求
  创建事件循环线程或连接池。
- 取消是协作式请求，恢复也可能因 CAS 竞争返回未接受；客户端必须读取最新记录，而非只依赖
  HTTP 成功码。
- 事件读取器可能返回包含完整检查点的对象。对外暴露前应按租户授权、最小字段原则和数据分类
  策略过滤。

## 当前限制

- 当前路由没有 `submit_human_response` 端点。
- `RunResponse` 不包含 `pending_interaction`、人工反馈历史、checkpoint revision 或事件序号。
  因此仅使用本包无法完成“暂停 → 提交人工反馈 → 恢复”的完整 HTTP 闭环；需要应用层另行提供
  受鉴权的反馈接口，或等待后续稳定集成 API。
- `RunResponse` 不返回 Core 的任意 metadata；这减少了泄漏面，但也意味着业务扩展字段需要独立
  设计版本化 DTO，不能依赖内部对象透传。
- 本包不包含 WebSocket/SSE、后台 Worker、数据库迁移、幂等键管理或管理后台。
