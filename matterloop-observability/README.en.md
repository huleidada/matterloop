[简体中文](https://github.com/huleidada/matterloop/blob/main/matterloop-observability/README.md) | English

# matterloop-observability

MatterLoop events are business facts, not log strings. `matterloop-observability` connects Core
`LoopEvent` objects to logs, metrics, and traces while leaving process-wide logging and
OpenTelemetry configuration to the host application.

```bash
pip install matterloop-observability
# When OpenTelemetry is required
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

Inject `events` into `AgentLoop(events=...)`. Handlers run in order. This package does not create a
background queue and does not own the Logger, Exporter, Provider, or their shutdown process.

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
- `TracingHandler` creates a short Span for each event; it does not automatically create a parent
  Span covering the entire Loop.

The OpenTelemetry components use the API only. The host must configure an SDK, Exporter, sampling,
and resource attributes before use. Installing the extra does not make data export automatically.
Constructing these components without the OpenTelemetry dependency raises `RuntimeError`
immediately.

## Extension points

Connect a synchronous or asynchronous callable with `HandlerEventPublisher(handler)`. For
batching, retries, or backpressure, implement Core `EventPublisher.publish(event)` directly and
manage a bounded queue and shutdown procedure inside that implementation.

This package currently targets Core `LoopEvent`. TeamLoop events have a different data structure
and require a dedicated adapter; a team event publisher cannot be passed directly to these
handlers. See the [Enterprise Integration Guide](../docs/enterprise-integration.en.md) for
production topology and shutdown order.
