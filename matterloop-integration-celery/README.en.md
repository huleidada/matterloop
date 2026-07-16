[简体中文](https://github.com/huleidada/matterloop/blob/main/matterloop-integration-celery/README.md) | English

# matterloop-integration-celery

Celery already provides Broker, message acknowledgement, and redelivery semantics. This package
only wires the two sides together: the API process sends versioned MatterLoop commands, while a
Worker process reconstructs the Runtime and claims runs through a shared `RunRepository`.

```bash
pip install matterloop-integration-celery
```

## It is not a pull-based QueueBackend

`CeleryQueueProducer` implements `QueueProducer`. The compatibility name `CeleryQueueBackend`
refers to the same push adapter, but it has no `lease/acknowledge/release` methods. Do not start a
MatterLoop pull Worker to consume the same commands.

```text
API / QueueRuntime
  ├─ First persist RunRecord in the shared repository
  └─ CeleryQueueProducer.send_task(JSON)
                 │
                 ▼
Celery Worker
  ├─ Strictly decode the message
  ├─ Invoke the factory to create Runtime and repository
  ├─ CAS: QUEUED → RUNNING
  ├─ runtime.run() / runtime.resume()
  └─ CAS: RUNNING → terminal state or PAUSED
```

A deterministic Celery task ID helps revocation and troubleshooting, but it does not replace
repository CAS.

## API process

```python
from matterloop_integration_celery import CeleryQueueProducer
from matterloop_runtime import QueueRuntime

producer = CeleryQueueProducer(app=celery_app, queue="matterloop", codec=None)
runtime = QueueRuntime(producer=producer, repository=shared_repository)
```

`CeleryQueueProducer(app, queue, codec)` wraps synchronous `send_task()` in a thread so it does not
block the event loop. The start task is named `matterloop.run`, the resume task is named
`matterloop.resume`, and messages always use the JSON serializer.

`cancel(run_id)` sends `revoke(terminate=False)` for the three deterministic task IDs covering the
start, continue-resume, and replan-resume operations. Success means only that the revocation
request was submitted; it does not prove that currently executing code has stopped.

## Worker process

```python
from matterloop_integration_celery import (
    CeleryWorkerDependencies,
    register_tasks,
)

register_tasks(
    celery_app=celery_app,
    runtime_factory_path="my_project.worker:create_dependencies",
)


def create_dependencies() -> CeleryWorkerDependencies:
    return CeleryWorkerDependencies(
        runtime=create_runtime(),
        repository=create_shared_repository(),
        closer=create_closer(),
        claim_lease_seconds=3600.0,
    )
```

`register_tasks(celery_app, runtime_factory_path)` registers two synchronous Celery tasks only. The
factory path must be trusted `module:callable` configuration; never accept it from a request or
message. Every delivery invokes the factory inside a new `asyncio.run()` event loop. Do not return
connections, locks, or clients bound to an old event loop.

The factory must return
`CeleryWorkerDependencies(runtime, repository, closer, claim_lease_seconds)`.
`claim_lease_seconds` defaults to 3600 seconds. `closer` runs after successful completion, duplicate
delivery, and failure. The returned `RegisteredCeleryTasks(run, resume)` is mainly useful for
startup diagnostics and tests.

## Message boundary

A start message contains only `run_id` and a schema-v1 `LoopRequest`: goal, acceptance criteria,
limits, and JSON metadata. A resume message contains only `run_id` and `continue/replan`. Runtime,
models, tools, clients, checkpoints, and human-interaction objects never enter the Broker.

`CeleryMessageCodec` rejects unknown fields, unknown schemas, NaN, Infinity, and arbitrary Python
objects. JSON safety does not make the content non-sensitive. Goals and metadata still require
Broker TLS, ACLs, retention limits, and log redaction.

## Handling duplicate delivery

Shared `RunRepository.compare_and_set()` is the only authority for execution ownership:

1. The Worker reads `RunRecord` and verifies that the message request matches the repository
   request.
2. Only `QUEUED`, or `RUNNING` beyond its claim lease, can be acquired through CAS.
3. After the Runtime returns, the Worker uses another CAS against the claimed version to persist
   the result.
4. CAS failure means another controller has advanced the state. The current Worker returns a
   duplicate diagnostic and does not overwrite the newer state.

The claim lease compares only `RunRecord.updated_at`; there is no heartbeat. If a task legitimately
runs longer than the lease, another Worker may take over and execute concurrently with the first.
The final CAS prevents state overwrite, but cannot undo external effects such as email, payment, or
file writes. Tools and business writes must use the run/task ID as an idempotency key.

Coordinate four time boundaries together: claim lease, Broker visibility timeout, Celery time
limit, and `LoopLimits.timeout_seconds`. The claim lease should cover worst-case end-to-end
execution time plus clock skew.

## Failure and ownership

- Invalid DTOs raise `CeleryPayloadError`; an invalid factory path or return type raises
  `CeleryFactoryError`.
- A mismatch between the message request and repository request raises `CeleryRunConflictError`.
- If the Runtime fails, the Worker attempts to CAS the record to `FAILED`. The repository stores
  only the exception type and a fixed summary. The original exception is still raised to Celery,
  so host logging must continue to redact it.
- The API and every Worker must use the same persistent repository. An in-process repository does
  not satisfy this requirement.
- This package does not close the Celery app or shared infrastructure. It closes only the `closer`
  explicitly returned by the factory.

Tasks enable late acknowledgement and worker-lost redelivery, intentionally providing at-least-once
semantics. This package does not configure autoretry, backoff, rate limits, soft or hard time limits,
Broker TLS, or result expiration.

## Current boundaries

The current transport supports starting and resuming Core Loops only. It does not provide team
runs, human-feedback submission, event queries, a claim heartbeat, or a persistent repository
implementation. The shared `RunRepository`, not the Celery result backend, is authoritative for the
complete `LoopResult`. See the
[Enterprise Integration Guide](../docs/enterprise-integration.en.md) for deployment composition
and shutdown order.
