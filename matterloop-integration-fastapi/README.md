# matterloop-integration-fastapi

这是一个薄 HTTP 适配层：校验请求、执行鉴权依赖、调用 Runtime、把领域错误映射成稳定状态码。
Loop 编排、持久化和 Worker 不会进入路由代码。

```bash
pip install matterloop-integration-fastapi
```

## 挂载路由

```python
from contextlib import asynccontextmanager

from fastapi import FastAPI, Header, HTTPException
from matterloop_integration_fastapi import create_router


async def authenticate(authorization: str | None = Header(default=None)) -> str:
    principal = await identity_service.authenticate(authorization)
    if principal is None:
        raise HTTPException(status_code=401, detail="unauthorized")
    return principal.subject


@asynccontextmanager
async def lifespan(_: FastAPI):
    yield
    await runtime.aclose()


app = FastAPI(lifespan=lifespan)
app.include_router(
    create_router(runtime=runtime, auth_dependency=authenticate, prefix="/loops")
)
```

`create_router(runtime, auth_dependency, prefix)` 只创建 `APIRouter`。Runtime 和外部客户端应在
lifespan 中构造、共享和关闭。

## 路由

| 方法 | 路径 | Queue Runtime | Direct Runtime |
| --- | --- | --- | --- |
| `POST` | `/loops/create` | 入队并返回运行视图 | 在请求内执行到完成或暂停 |
| `GET` | `/loops/list` | 分页查询 | `501` |
| `GET` | `/loops/{run_id}` | 查询单次运行 | `501` |
| `POST` | `/loops/{run_id}/cancel` | 请求协作式取消 | 转发取消请求 |
| `POST` | `/loops/{run_id}/resume` | 重新入队 | 在请求内恢复 |
| `GET` | `/loops/{run_id}/events/list` | 游标读取事件 | `501` |

生产 HTTP 服务通常应传入 `QueueRuntimeProtocol`。`DirectRuntimeProtocol` 会占用请求连接，也没有
可查询的运行目录。两种协议都是结构协议，不要求继承 MatterLoop 类型。

## 请求约束

创建请求的形状是：

```json
{
  "goal": "生成并验证发布说明",
  "acceptance_criteria": ["包含变更摘要", "验证链接有效"],
  "limits": {
    "max_cycles": 5,
    "max_attempts": 20,
    "max_steps_per_plan": 20,
    "timeout_seconds": 300
  },
  "metadata": {"tenant_id": "acme", "trace_id": "..."},
  "run_id": "release-note-2026-07-16"
}
```

HTTP DTO 使用严格 Pydantic 配置，未知字段直接拒绝。`run_id` 最长 128 字符，并限制为可安全用于
内部标识的字符；它仍然不是授权凭据。`metadata` 只允许 JSON 值，但不会自动做租户校验或脱敏。

恢复请求 `ResumeLoopRequest(mode)` 默认 `continue`；传 `replan` 才丢弃当前计划重新规划。

## 响应不是内部对象透传

`RunResponse` 只公开运行状态、输出和步骤记录，不返回任意 Core metadata。模型输出、验证证据和
artifact URI 仍可能敏感，API 网关应按数据分类继续过滤。

<details>
<summary>HTTP DTO 字段速查</summary>

- `LoopLimitsRequest(max_cycles, max_attempts, max_steps_per_plan, timeout_seconds)`。
- `CreateLoopRequest(goal, acceptance_criteria, limits, metadata, run_id)`。
- `ResumeLoopRequest(mode)`。
- `PlanStepResponse(step_id, description, executor, acceptance_criteria, requires_approval)`。
- `ArtifactResponse(name, uri, media_type)`。
- `ExecutionResponse(output, artifacts)`。
- `VerificationResponse(passed, feedback, score, evidence, failed_criteria)`。
- `IterationResponse(cycle, step_index, attempt, step, execution, verification)`。
- `RunResponse` 包含 `run_id`、`status`、`output`、`cycles`、`total_attempts`、`completed_steps`、
  `records`、`stop_reason`、`error`、`goal`、`version`、`created_at` 和 `updated_at`。
- `CancelResponse(run_id, accepted)`、`ResumeResponse(accepted, run)`、`EventListResponse(items)`。

`CancelResponse.accepted=True` 只说明请求被接收，不说明用户代码已经停止。Queue 模式下的
`ResumeResponse.run` 也通常仍处于 queued 状态。

</details>

## 错误契约

| 状态码 | 条件 |
| ---: | --- |
| `400` | Runtime 或领域参数非法、其他可处理的 MatterLoop 错误 |
| `404` | 运行不存在 |
| `409` | 重复 run ID、非法状态转换或 CAS 冲突 |
| `422` | Pydantic 的 body/path/query 校验失败 |
| `501` | 当前 Runtime 没有运行仓储或事件目录 |
| `503` | Runtime 已关闭或暂不可用 |

路由错误使用固定文案，不回传捕获异常的文本。但成功响应里的 `RunResponse.error` 来自运行记录，
应用在持久化前仍需清理供应商正文、内部路径和凭据。

## 上线前需要补上的部分

- `auth_dependency` 只是认证挂载点。每个端点还要校验当前主体是否拥有对应 `run_id`。
- 网关负责请求体大小、速率限制、CORS/CSRF、审计和租户级预算。
- 当前没有提交人工反馈的 HTTP 路由，`RunResponse` 也不暴露 `pending_interaction`。仅靠本包无法完成
  HTTP HITL 闭环。
- 当前没有 SSE/WebSocket、数据库迁移、Worker 或管理后台。

队列部署拓扑见[企业集成指南](../docs/enterprise-integration.md)。
