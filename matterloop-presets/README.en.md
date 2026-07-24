[简体中文](https://github.com/huleidada/matterloop/blob/main/matterloop-presets/README.md) | English

# matterloop-presets

Presets are composition roots with explicit trade-offs. They connect models, Agents, tools,
policies, checkpoints, and Runtimes without hiding those components or presenting development
configuration as a production solution.

```bash
pip install matterloop-presets
```

## Choosing a preset

| Use case | Builder | Default capabilities | What you must provide |
| --- | --- | --- | --- |
| Model-only tasks and tests | `build_minimal_runtime` | No tools; in-memory checkpoint | One `ModelClient` |
| Controlled code changes | `build_coding_runtime` | Read-only files; file writes and allowlisted commands after approval | Model, workspace, and approval policy |
| Research that requires sources | `build_research_runtime` | Read-only files, HTTPS GET, and a citation threshold | Model, workspace, and host allowlist |
| Separate API and Worker | `build_production_runtime` | Queue facade, external checkpoint, and fail-closed audit | All production infrastructure |

The corresponding synchronous entry points are `build_minimal_local_runtime`,
`build_coding_local_runtime`, `build_research_local_runtime`, and
`build_production_local_runtime`. Synchronous versions use a dedicated event-loop thread; they are
not a separate orchestration implementation.

The four asynchronous Builders accept the following key arguments: minimal accepts `model` and an
optional `config`; coding accepts `model`, `workspace`, an optional `config`, and `approval_gate`;
research accepts `model`, `workspace`, and the required `config`; production accepts `model`, an
optional `config`, four required infrastructure components, and optional `event_reader` and
`approval_gate` values.

## Getting started

```python
from matterloop_core import LoopRequest
from matterloop_presets import build_minimal_runtime

async with build_minimal_runtime(model=model_client) as runtime:
    result = await runtime.run(LoopRequest(goal="Prepare the release checklist"))
```

The application constructs the model client. A Preset does not read `.env` and does not create an
OpenAI, DeepSeek, Qwen, Zhipu, or MiniMax SDK client.

## Coding: permissions by executor

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

The `default` executor can only read files. `privileged_executor` additionally receives file-write
and restricted Shell capabilities. Steps assigned to the privileged executor are forcibly marked
as requiring approval. If `approval_gate` is omitted, approval is deferred and the Loop pauses; it
does not allow the step by default. Shell commands do not pass through a command interpreter and
do not inherit the host environment. This is still not an isolation boundary for malicious code.

## Research: restrict access without endorsing sources

```python
from matterloop_presets import ResearchPresetConfig, build_research_runtime

runtime = build_research_runtime(
    model=model_client,
    workspace="/srv/reference-data",
    config=ResearchPresetConfig(allowed_hosts=frozenset({"docs.example.com"})),
)
```

HTTP is restricted to HTTPS `GET`, an exact host allowlist, and no redirects.
`require_citation=True` checks only for a URL or artifact reference in the result; it does not prove
that a source is authentic, current, or trustworthy.

## Production: assemble components without running the Worker for you

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

If any of `queue_backend`, `run_repository`, `checkpoint_store`, or `audit_publisher` is missing,
the Builder raises `PresetConfigurationError`; it never falls back to an in-memory implementation.
The returned `ProductionRuntime` contains the control-plane `queue_runtime` and execution-plane
`worker_runtime`. The deployment remains responsible for leases, acknowledgements, lease renewal,
dead letters, and the Worker loop.

`trace_exporter` is optional: when a `SpanExporter` (such as `JsonlExporter` or `OtelExporter`) is
provided, the preset attaches a `TraceBuilder` to the audit event pipeline, wraps the model client
in a `TracedModelClient`, and drains the export pipeline on `ProductionRuntime.aclose()`. By
default no tracing resources are created and the event pipeline behaves exactly as before.

## Configuration reference

All configuration types are frozen dataclasses:

- `AgentPresetConfig(model_name, max_plan_steps, max_tool_rounds, pass_score, max_identical_feedback, retry)`
  defaults to `"default"`, `20`, `8`, `80`, `2`, and the default retry configuration,
  respectively.
- `MinimalPresetConfig` and `ProductionPresetConfig` currently add no fields.
- `CodingPresetConfig` adds `privileged_executor`, `allowed_commands`, `shell_environment`,
  `max_read_bytes`, `max_write_bytes`, `max_shell_timeout_seconds`, and
  `max_shell_output_bytes`. The privileged executor defaults to `"coding"`; the command set
  defaults to `pytest/ruff`; file and output limits default to 1,000,000 bytes; and commands may
  run for at most 60 seconds.
- `ResearchPresetConfig` adds `allowed_hosts`, `max_read_bytes`, `max_response_bytes`,
  `max_http_timeout_seconds`, and `require_citation`. The host set is required in practice. The
  defaults allow 1,000,000 bytes per file, 2,000,000 bytes per response, a 20-second request, and
  require a citation.

`max_plan_steps` and `max_tool_rounds` are not total budgets. Cycles, attempts, step counts, and
active timeout come from `LoopRequest.limits`. Token, cost, and concurrency budgets must be
configured separately through `matterloop-policies`.

## Lifecycle and boundaries

Use asynchronous Runtimes with `async with`/`aclose()` and synchronous Runtimes with
`with`/`close()`. A Preset closes the model adapters and tool registry it registers itself. It does
not close external queues, repositories, Redis clients, or audit backends. Components introduced
through hot replacement also require the caller to manage retirement and shutdown.

When an application needs multiple model roles, persistent budgets, domain-specific verifiers, or
custom tool authorization, assemble the foundational packages directly. Adding more Preset
configuration generally makes ownership harder to understand. See the
[Enterprise Integration Guide](../docs/enterprise-integration.en.md) for a complete composition.
