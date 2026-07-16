[简体中文](CHANGELOG.md) | English

# Changelog

This file records user-visible MatterLoop changes. The repository's 12 distributions share one version, so each
version entry covers the complete component set instead of maintaining separate changelogs that can drift apart.

## [Unreleased]

### Added

- Added complete English mirrors, bidirectional language switches, and internationalization contract tests for all
  public Markdown documentation.

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

[Unreleased]: https://github.com/huleidada/matterloop/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/huleidada/matterloop/releases/tag/v0.1.0
