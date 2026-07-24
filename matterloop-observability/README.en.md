[简体中文](https://github.com/huleidada/matterloop/blob/main/matterloop-observability/README.md) | English

# matterloop-observability

MatterLoop events are business facts, not log strings. `matterloop-observability` connects Core
`LoopEvent` objects to logs, metrics, tree-shaped traces, and scores while leaving process-wide
logging and OpenTelemetry configuration to the host application.

```bash
pip install matterloop-observability
# When OpenTelemetry export is required (includes the SDK and the OTLP/HTTP exporter)
pip install "matterloop-observability[otel]"
```

## A practical assembly

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

Inject `events` into `AgentLoop(events=...)`. Handlers run in order. Synchronous handlers create no
background queue and do not own the Logger or its shutdown. The only exception is the
`BatchingPipeline` used for trace export below: it holds a background daemon thread that the caller
must `shutdown()` before the application exits.

## Choose the failure policy explicitly

`CompositeEventPublisher(publishers, failure_mode)` supports two policies:

- `LOG_AND_CONTINUE` is the default and fits disposable telemetry. If one publisher fails, the
  exception is logged and subsequent publishers still receive the event.
- `RAISE` stops at the first failure and fits cases in which audit records must not be lost. The
  trade-off is that an observability failure can interrupt the business Loop.

Calling several Publishers in sequence does not provide a transaction between state persistence
and audit persistence. If both must succeed atomically, use an outbox, durable event table, or
messaging system to provide the atomic handoff.

## What appears in logs

`StructuredLoggingHandler(logger, redactor)` emits one-line JSON containing the event type,
`run_id`, Loop status, timestamp, event detail, and request metadata. The default Logger name is
`matterloop.events`. The application remains responsible for log formatting, rotation, retention,
and access control.

`Redactor(extra_fields)` recursively inspects mapping keys. By default it recognizes `token`,
`authorization`, `cookie`, `api_key`, `password`, and `secret`, including prefixed or suffixed names
such as `access_token`. It does not scan free-form text. Secrets may still leak through prompts,
model output, URL query parameters, or exception traces. Never place credentials in `goal`,
`detail`, or arbitrary string metadata.

## Metrics and traces

- `MetricsHandler` keeps in-process event counters for tests and lightweight diagnostics.
- `OpenTelemetryMetricsHandler` writes to `matterloop.loop.events` and attaches only the event type
  and Loop status.
- `TracingHandler` is deprecated: it creates an isolated short Span per event and cannot rebuild
  parent-child relationships. Use the `TraceBuilder` below instead; it will be removed in a future
  release.

`OpenTelemetryMetricsHandler` and `TracingHandler` use the API only. The host must configure an
SDK, exporter, sampling, and resource attributes first; constructing them without the dependency
raises `RuntimeError` immediately. `OtelExporter` is the exception: it ships its own SDK and
OTLP/HTTP exporter (provided by the `[otel]` extra) and raises `ImportError` when they are missing.

## Tree-shaped traces and scores

`TraceBuilder(pipeline)` implements the Core `EventPublisher` protocol and rebuilds the lifecycle
event stream into a tree-shaped span hierarchy: a root span covers the whole run, while execution,
verification, iteration snapshots, and overall completion evaluation each get their own span. When
the verification span closes, `VerificationResult.score` (0-100) is normalized into a `Score`.
Closed spans and scores enter `BatchingPipeline(exporter, flush_at, flush_interval)`, where a
background daemon thread batches them for the `SpanExporter`.

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
# Before the application exits: pipeline.shutdown()
```

`JsonlExporter(path)` appends one JSON record with a `type` field per line and has no extra
dependencies. `OtelExporter(endpoint)` rebuilds spans on an OTLP/HTTP backend with the original
parent-child relationships and timings, and exports each score as an instant child span named
`score:<name>` on the same trace. The SDK creates the actual OTel trace/span IDs; MatterLoop's
`run_id`, `span_id`, and parent ID are retained as `matterloop.trace_id`, `matterloop.span_id`, and
`matterloop.parent_span_id` attributes. The pipeline queue is bounded (10000 by default); when full,
new items are dropped with a warning. OTel waits for the root span to build parent contexts through
the public API, and buffers at most 10000 records per run by default before dropping new records with
a warning. A failed export is retried once and then dropped. No exception is ever propagated back
into the Loop.

`SpanRecord` is an immutable span record: `trace_id` (the `run_id` that produced the span),
`span_id`, `parent_span_id`, `name`, `observation_type`, `started_at`, `ended_at`, `attributes`,
`level`, and `status_message`. `Score` is an immutable score: `name`, `value` (NUMERIC values are
normalized to 0-1), `data_type`, `source`, `run_id`, `step_id`, `comment`, `evidence`, and
`timestamp`. `score_from_verification` maps a verification result to a NUMERIC score;
`score_from_review` accepts any duck-typed review result with `score`/`summary`/`evidence`
attributes and does not require the agents package.

## Model call spans

```python
from matterloop_observability import wrap_model_client

client = wrap_model_client(model_client, trace_builder)
```

`TracedModelClient(client, trace_builder, pipeline)` wraps any `ModelClient`: when the request
metadata contains a `run_id`, it records a generation span carrying the redacted input messages,
sampling parameters, output text, and six token usage fields. The parent span is resolved by the
`trace_builder` from `run_id`/`step_id` and falls back to the run root span. When the metadata has
no `run_id`, the call passes straight through; observability never blocks a call. A model error is
recorded as an ERROR span and re-raised unchanged. The Planner, Worker, Verifier, and Reviewer in
the agents package already write `run_id`, `step_id`, and `agent` into request metadata, so
wrapping the client registered in the `ModelRegistry` yields model spans automatically. The
production preset can wire all of this through its `trace_exporter` parameter; see
[matterloop-presets](../matterloop-presets/README.en.md).

## Extension points

Connect a synchronous or asynchronous callable with `HandlerEventPublisher(handler)`. Batching,
retries, and backpressure for spans and scores are already provided by `BatchingPipeline`. For a
custom event destination, implement Core `EventPublisher.publish(event)` directly and manage a
bounded queue and shutdown procedure inside that implementation.

This package currently targets Core `LoopEvent`. TeamLoop events have a different data structure
and require a dedicated adapter; a team event publisher cannot be passed directly to these
handlers. See the [Enterprise Integration Guide](../docs/enterprise-integration.en.md) for
production topology and shutdown order.
