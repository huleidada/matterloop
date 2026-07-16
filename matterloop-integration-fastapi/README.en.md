[简体中文](https://github.com/huleidada/matterloop/blob/main/matterloop-integration-fastapi/README.md) | English

# matterloop-integration-fastapi

This is a thin HTTP adapter: it validates requests, runs the authentication dependency, invokes a
Runtime, and maps domain errors to stable status codes. Loop orchestration, persistence, and Worker
execution do not belong in the routing layer.

```bash
pip install matterloop-integration-fastapi
```

## Mounting the router

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

`create_router(runtime, auth_dependency, prefix)` creates an `APIRouter` only. Construct, share,
and close the Runtime and external clients through the application lifespan.

## Routes

| Method | Path | Queue Runtime | Direct Runtime |
| --- | --- | --- | --- |
| `POST` | `/loops/create` | Enqueue and return the run view | Execute within the request until completion or pause |
| `GET` | `/loops/list` | Paginated query | `501` |
| `GET` | `/loops/{run_id}` | Query one run | `501` |
| `POST` | `/loops/{run_id}/cancel` | Request cooperative cancellation | Forward the cancellation request |
| `POST` | `/loops/{run_id}/resume` | Enqueue again | Resume within the request |
| `GET` | `/loops/{run_id}/events/list` | Read events by cursor | `501` |

A production HTTP service should generally supply `QueueRuntimeProtocol`. `DirectRuntimeProtocol`
occupies the request connection and has no queryable run directory. Both are structural protocols;
an implementation does not have to inherit a MatterLoop class.

## Request constraints

A create request has the following shape:

```json
{
  "goal": "Generate and verify release notes",
  "acceptance_criteria": ["Include a change summary", "Verify that links are valid"],
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

HTTP DTOs use strict Pydantic configuration, so unknown fields are rejected. `run_id` is limited to
128 characters and to characters safe for an internal identifier; it is still not an authorization
credential. `metadata` accepts JSON values only, but is not automatically tenant-validated or
redacted.

`ResumeLoopRequest(mode)` defaults to `continue`. Pass `replan` to discard the current plan and
plan again.

## Responses are not raw internal objects

`RunResponse` exposes only run status, output, and step records. It does not return arbitrary Core
metadata. Model output, verification evidence, and artifact URIs may still be sensitive, so the API
gateway should continue filtering them according to the application's data classification.

<details>
<summary>HTTP DTO field reference</summary>

- `LoopLimitsRequest(max_cycles, max_attempts, max_steps_per_plan, timeout_seconds)`.
- `CreateLoopRequest(goal, acceptance_criteria, limits, metadata, run_id)`.
- `ResumeLoopRequest(mode)`.
- `PlanStepResponse(step_id, description, executor, acceptance_criteria, requires_approval)`.
- `ArtifactResponse(name, uri, media_type)`.
- `ExecutionResponse(output, artifacts)`.
- `VerificationResponse(passed, feedback, score, evidence, failed_criteria)`.
- `IterationResponse(cycle, step_index, attempt, step, execution, verification)`.
- `RunResponse` contains `run_id`, `status`, `output`, `cycles`, `total_attempts`,
  `completed_steps`, `records`, `stop_reason`, `error`, `goal`, `version`, `created_at`, and
  `updated_at`.
- `CancelResponse(run_id, accepted)`, `ResumeResponse(accepted, run)`, and
  `EventListResponse(items)`.

`CancelResponse.accepted=True` means only that the request was accepted; it does not mean user code
has stopped. In Queue mode, `ResumeResponse.run` will also usually remain in the queued state.

</details>

## Error contract

| Status | Condition |
| ---: | --- |
| `400` | Invalid Runtime or domain arguments, or another recoverable MatterLoop error |
| `404` | Run does not exist |
| `409` | Duplicate run ID, invalid state transition, or CAS conflict |
| `422` | Pydantic body, path, or query validation failed |
| `501` | Current Runtime has no run repository or event directory |
| `503` | Runtime is closed or temporarily unavailable |

Route errors use fixed messages and never return the text of a caught exception. However,
`RunResponse.error` in a successful response comes from the run record. Before persistence, the
application must still remove provider content, internal paths, and credentials.

## Required production work

- `auth_dependency` is only the authentication hook. Every endpoint must also verify that the
  current principal owns or may access the corresponding `run_id`.
- The gateway is responsible for request-size limits, rate limits, CORS/CSRF, auditing, and
  tenant-level budgets.
- There is currently no HTTP route for submitting human feedback, and `RunResponse` does not expose
  `pending_interaction`. This package alone cannot provide a complete HTTP HITL loop.
- There is currently no SSE/WebSocket transport, database migration, Worker, or administration UI.

See the [Enterprise Integration Guide](../docs/enterprise-integration.en.md) for queued deployment
topologies.
