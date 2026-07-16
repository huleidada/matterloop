[简体中文](https://github.com/huleidada/matterloop/blob/main/matterloop-runtime/README.md) | English

# matterloop-runtime

Runtime turns a Loop into an application-facing service boundary: asynchronous execution, synchronous bridging,
queue control plane, component lifecycle, and local process execution. It does not decide how an Agent plans, and it
does not provide a sandbox that can safely run arbitrary code.

```bash
pip install matterloop-runtime
```

## Three entry points

### Asynchronous applications

```python
from matterloop_runtime import AsyncRuntime

async with AsyncRuntime(engine=agent_loop, resources=(model_client, tool_registry)) as runtime:
    result = await runtime.run(request)
```

`AsyncRuntime(engine, resources)` closes `resources` in reverse order. It does not take ownership of an object merely
because `engine` references that object.

### Synchronous applications

```python
from matterloop_runtime import LocalRuntime

with LocalRuntime(runtime=async_runtime, thread_name="matterloop-runtime") as runtime:
    result = runtime.run(request)
```

The `runtime` argument of `LocalRuntime` is the asynchronous facade to bridge, and `thread_name` defaults to
`"matterloop-runtime"`. It uses a dedicated event-loop thread, is intended to be long-lived and reused, and must be
closed before process exit. Do not create it for each FastAPI request, and do not call synchronous methods from its
own event-loop thread.

### Queue control plane

```python
from matterloop_runtime import QueueRuntime

runtime = QueueRuntime(
    producer=queue_producer,
    repository=run_repository,
    event_reader=event_reader,
)
run_id = await runtime.submit(request)
record = await runtime.wait(run_id, timeout_seconds=30)
```

`QueueRuntime(producer, repository, event_reader)` provides submit/get/list/wait/cancel/resume/result operations and
event queries; it does not execute jobs. A separate Worker must consume commands, invoke `AsyncRuntime`, commit the
result through repository CAS, and then acknowledge or release the message.

## Human feedback

Runtime only forwards Core semantics:

```python
await runtime.submit_human_response(run_id, response)
result = await runtime.resume(run_id)  # Continues precisely by default
```

Submitting feedback does not resume a run implicitly. Pass `ResumeMode.REPLAN` when replanning is required.

## Two queue concurrency boundaries

A `QueueBackend` lease determines which Worker temporarily holds a message. `RunRepository` version CAS determines
who can commit the latest state. Both are required. Holding a lease does not grant globally unique ownership of
external side effects.

- `QueueProducer` provides only `enqueue/cancel` and suits push systems such as Celery.
- `QueueBackend` also provides `lease/acknowledge/release` for actively polling Workers.
- `RunRepository` provides `create/get/list/compare_and_set`.
- `RunEventReader` reads events in pages using an exclusive cursor.

`InMemoryQueueBackend` and `InMemoryRunRepository` are intended only for tests and single-process development. An
expired lease is recovered during the next `lease()` call; there is no heartbeat, dead-letter queue, or cross-process
notification.

<details>
<summary>Queue data structure reference</summary>

- `QueuedRun(run_id, action, request, resume_mode, enqueued_at)`: `START` must carry a request; `RESUME` must not carry
  one.
- `QueueLease(lease_id, job, worker_id, expires_at, attempt)`: `attempt` is a message-delivery count, not a Core
  Executor attempt.
- `RunRecord(run_id, request, status, version, result, error, created_at, updated_at)`: `version` starts at 0, and a CAS
  replacement must increment it by exactly 1.
- `QueueAction`: `START`, `RESUME`.
- `RunStatus`: `QUEUED`, `RUNNING`, `PAUSED`, `BLOCKED`, `COMPLETED`, `FAILED`, `CANCELLED`, `TIMED_OUT`.

`wait()` also returns for PAUSED and BLOCKED because they are settled states. Only completed, failed, cancelled, and
timed_out are terminal states.

</details>

## Safe hot replacement

`RuntimeContainer.acquire(name)` pins a component instance for a long-running call. `replace(name, component)` starts
the new instance before making it visible to new calls; the old instance closes after existing leases exit.
`get(name)` returns only an instantaneous snapshot and is not suitable for a long transaction across `await`.

```python
async with container.acquire("model") as model:
    await model.generate(request)

await container.replace("model", replacement)
```

`register(name, component, replace=False)`, `unregister(name)`, `names()`, and `aclose()` form the complete lifecycle.
Initial components supplied to the constructor are treated as already started. In the current implementation, a
failure while closing the old component propagates even though replacement has taken effect. Component shutdown
should be idempotent; after an error, callers should inspect container state before deciding whether to retry.

## Local process execution

```python
from matterloop_runtime import LocalProcessSandbox, ProcessRequest

sandbox = LocalProcessSandbox(
    root="/srv/workspaces/job-42",
    base_environment={"PATH": "/opt/matterloop/bin:/usr/bin"},
)
result = await sandbox.run(
    ProcessRequest(
        argv=("python", "-m", "pytest"),
        cwd=".",
        timeout_seconds=60,
        max_output_bytes=1_000_000,
    )
)
```

`ProcessRequest(argv, cwd, environment, stdin, timeout_seconds, max_output_bytes)` is passed directly to
`create_subprocess_exec` and does not use a Shell. The default environment is empty, the default timeout is 30
seconds, and stdout/stderr share a 1,000,000-byte budget.
`ProcessResult(return_code, stdout, stderr, duration_seconds, timed_out, truncated)` describes the execution result
and truncation state.

`LocalProcessSandbox` limits only the initial cwd, environment, wait time, and retained output. It does not limit
system calls, networking, CPU, memory, user privileges, accessible files, or the process tree. `root` also does not
stop a program from reading outside that directory after startup. Untrusted code should run in a container, virtual
machine, or remote sandbox that implements the `Sandbox` protocol.

## Failures and shutdown

A closed facade raises `RuntimeClosedError`; a duplicate run ID raises `DuplicateRunError`; attempts to resume a
missing or non-resumable run raise `RunNotFoundError` and `RunNotResumableError`; cwd escape raises
`SandboxPathError`.

Runtime does not read environment variables or create Redis, Celery, database, or model clients. The recommended
shutdown order is: stop accepting new requests, stop delivery, drain or cancel Workers, release leases, close Runtime,
and finally close connection pools owned by the host. See the
[Enterprise integration guide](../docs/enterprise-integration.en.md) for more deployment constraints.
