[简体中文](CHANGELOG.md) | English

# Changelog

This file records user-visible MatterLoop changes. The repository's 12 distributions share one version, so each
version entry covers the complete component set instead of maintaining separate changelogs that can drift apart.

## [Unreleased]

## [0.2.3] - 2026-08-03

### Fixed

- Runtime semantic compaction no longer asks the model to echo internal context item IDs. The
  runtime now writes trusted source IDs from the original input, preventing a hard-threshold
  `context compaction failed` error when a model omits or rewrites an ID.

## [0.2.2] - 2026-07-29

### Added

- Added a composable intent-resolution architecture to Agents. Strongly typed candidates, priorities,
  confidence values, and conflict thresholds resolve multi-intent inputs while explicitly reporting
  uncertainty when signals are insufficient or candidates conflict.
- Added a pluggable scoring architecture to Agents with per-criterion evidence, weights, minimum scores,
  required criteria, and normalized totals. Different task types can select appropriate scoring policies
  without hard-coding business-specific acceptance fields into the generic runtime.

### Changed

- `CriteriaVerifier` now produces structured per-criterion evaluations and delegates its final decision to
  an injected scoring policy. Verification prompts and runtime protocol fields consistently use English.

## [0.2.1] - 2026-07-29

### Added

- Added `OpenTelemetryToolMiddleware` to record Tool, Skill, and MCP calls. By default it records only
  payload size, SHA-256, and allowlisted semantic attributes. Verbatim bounded payload capture requires
  explicit `capture_tool_payloads=True` (4096 bytes by default and configurable through
  `capture_max_body_bytes`); the production preset accepts explicit `tools` and `tool_authorizer` inputs.
- Added live OTel topology for Team and child-Agent execution. Team snapshots persist the W3C carrier,
  preserving `matterloop.team -> matterloop.team.agent -> matterloop.run` parentage across pauses,
  blocks, and cross-process resume.

### Changed

- Checkpoint schema v2 now contains both `propagation_context` and `external_state_refs` while retaining
  read compatibility with legacy unversioned v1 payloads.
- Standardized trace span names on fixed `matterloop.*` semantics and moved dynamic agent/executor data to
  attributes. `OtelExporter.aclose()` now force-flushes every Provider and shuts down only an owned Provider.
  This is an incompatible observability-schema change; existing dashboards, alerts, and queries must
  migrate to the new fixed Span names.
- Runtime close now drains in-flight Loop and tool calls before closing tools, models, and exporters, so a
  late-ending `matterloop.tool` span cannot be lost after its Provider has shut down.

## [0.2.0] - 2026-07-28

### Added

- Added the Runtime-native Context Lifecycle Engine for token pressure, tool-result externalization,
  native/semantic compaction, immutable snapshots, Redis CAS, long-run recovery, and opt-in memory
  extraction without adding a distribution.
- Added canonical context input, model context scopes, exact token counting, and native compaction
  protocols to Models; all built-in Agent and Team model calls now carry stable scopes.
- Added tree-shaped tracing and scoring to observability: `TraceBuilder` rebuilds the lifecycle event
  stream into a span tree, `BatchingPipeline` batches `SpanRecord` and `Score` exports to JSONL or OTLP/HTTP
  exporters, and `TracedModelClient` wraps a model client to record generation spans automatically.
- Added an optional `trace_exporter` parameter to the production preset that attaches the TraceBuilder to the
  audit event pipeline and wraps the model client, draining the export pipeline when the runtime closes.
- Added the public lifecycle event `LoopEventType.COMPLETION_EVALUATION_COMPLETED` to Core, emitted after
  the whole-run acceptance decision (accept/replan/request human) so subscribers know exactly when the
  evaluation ends.
- Added live OTel tracing to observability: `OpenTelemetryTracePublisher` maintains real span contexts
  during Loop execution and `OpenTelemetryModelClient` records nested generation spans, so database/HTTP
  auto-instrumentation joins the same trace; a block or pause persists W3C `traceparent`/`tracestate` in
  the same checkpoint CAS and resume creates a real child Span, while `run_id` remains business correlation.
- Added the Core `CheckpointPreparer` protocol and `LoopContext.propagation_context`, allowing event
  publishers to place durable correlation data such as W3C propagation context into the checkpoint CAS;
  `CompositeEventPublisher` forwards the hook.

### Changed

- Upgraded checkpoints to schema v2 with `external_state_refs` while retaining read compatibility
  with legacy unversioned v1 payloads.

### Deprecated

- Deprecated `TracingHandler`; its isolated short spans are superseded by the tree traces from `TraceBuilder`.

## [0.1.2] - 2026-07-23

### Added

- Failure Analysis Engine that attributes stop reasons, verification feedback, and error patterns, and generates
  correction strategies for the next loop.
- Evaluation Framework with benchmark, golden, and regression datasets, plus Agent, Runtime, and domain metrics and
  an evaluation loop.
- Learning Loop and `LoopEngineeringRuntime` for failure learning, strategy optimization, experience reuse, and
  multi-round engineering loops.
- Agent Communication Model with Contract schema validation, a message bus, and a managed registry for capabilities,
  versions, and SLA.
- Four-layer Memory reference implementations: Working, Episodic, Semantic (vectors and knowledge graphs), and
  Procedural.
- Event Bus, Event Router, lifecycle handler helpers, and cost tracking aggregated by run and tenant.
- Execution Ledger, idempotent invocation, transactional checkpoints, and a horizontally scalable QueueWorker.
- MCP Governance with a unified gateway, risk-tiered policies, three-dimensional access control, quotas, and audit.
- Multi-tenant isolation, token authentication, role-based authorization, and data-access policies.

## [0.1.1] - 2026-07-21

### Added

- Added complete English mirrors, bidirectional language switches, and internationalization contract tests for all
  public Markdown documentation.
- Added supervised Core heartbeats, prompt cancellation, crash recovery, and persistent Redis checkpoints.
- Added queue lease renewal, idempotent run submission, and terminal-state CAS protection.

### Changed

- Completed the FastAPI `httpx2` and MCP test dependencies, aligned development extras and internal lower bounds across all 12
  distributions, and strengthened lock-file gates.
- Execution results are checkpointed before verification; ambiguous in-flight work now blocks for reconciliation
  instead of being replayed automatically.

### Security

- Child Agents are forced into a read-only tool scope; Shell, file writes, non-GET HTTP, and unknown MCP capabilities
  remain under main-Loop governance.
- Tool effects are enforced by the Registry before authorization, and business metadata cannot elevate a child Agent
  to full access.

## [0.1.0] - 2026-07-16

### Added

- A pausable, resumable, replannable, and auditable Agent Loop with structured human feedback and checkpoint CAS.
- A DAG-based TeamLoop with multi-Agent capability routing, parallel execution, independent verification, and team
  review.
- A model registry and provider adapter layer covering OpenAI, DeepSeek, Qwen, Zhipu, and MiniMax while retaining a
  custom `ModelClient` interface.
- Hierarchical quota ledgers for models, tools, Agent tasks, and estimated cost.
- MCP, Skills, Shell, filesystem, and HTTP tool integration with approval and permission extension points.
- Asynchronous, local synchronous, and queue runtimes, plus FastAPI, Celery, and Redis integration packages.
- `minimal`, `coding`, `research`, and `production` presets with offline enterprise examples.

### Security

- Applications construct and inject SDK clients and credentials; distributions do not read `.env` or store API keys.
- Model continuation and reasoning data do not enter public results; logs and events support sensitive-field
  redaction.
- Shell tools execute argv directly, while filesystem and HTTP tools enforce path, protocol, host, and response-size
  boundaries.

[Unreleased]: https://github.com/huleidada/matterloop/compare/v0.2.3...HEAD
[0.2.3]: https://github.com/huleidada/matterloop/releases/tag/v0.2.3
[0.2.2]: https://github.com/huleidada/matterloop/releases/tag/v0.2.2
[0.2.1]: https://github.com/huleidada/matterloop/releases/tag/v0.2.1
[0.2.0]: https://github.com/huleidada/matterloop/releases/tag/v0.2.0
[0.1.2]: https://github.com/huleidada/matterloop/releases/tag/v0.1.2
[0.1.1]: https://github.com/huleidada/matterloop/releases/tag/v0.1.1
[0.1.0]: https://github.com/huleidada/matterloop/releases/tag/v0.1.0
