[简体中文](README.md) | English

# Offline composition examples

These are not "Hello World" snippets. They are four directly runnable composition roots built with
Fake or in-memory components. They do not read environment variables, require credentials, or
connect to a model service, Broker, or Redis.

```bash
uv run python -m examples.enterprise.embedded_agent
uv run python -m examples.enterprise.team_collaboration
uv run python -m examples.enterprise.queued_service
uv run python -m examples.enterprise.mcp_skills_tools
```

## Where to start

| Problem to solve | Example | Recommended focus |
| --- | --- | --- |
| Run a recoverable Agent inside an existing Python service | [`embedded_agent.py`](embedded_agent.py) | Runtime composition, human revision, budgets, and audit |
| Split work across Agents and perform team-level acceptance | [`team_collaboration.py`](team_collaboration.py) | DAG, capability routing, fan-out/fan-in, and Reviewer |
| Separate the API control plane from Workers | [`queued_service.py`](queued_service.py) | Lease, CAS, acknowledgement, and the Celery-or-Redis choice |
| Integrate an MCP Server and controlled Skill | [`mcp_skills_tools.py`](mcp_skills_tools.py) | Session injection, tool authorization, and resource and Prompt boundaries |

Each example intentionally keeps dependency construction together. Production applications will
usually place it in a FastAPI lifespan, Worker startup hook, or dependency-injection container, but
resource ownership and shutdown order should remain explicit.

## Replacing offline components

- Replace `FakeModelClient` with a provider adapter from `matterloop_models.providers`; the
  application must still create the SDK client.
- Replace in-memory checkpoint, TeamRepository, and RunRepository implementations with persistent
  implementations that provide CAS, leases, and backups.
- Before replacing example tools with real tools, add `ToolAuthorizer`, tenant authorization, and
  auditing. Do not expose Shell or network access directly.
- Celery is a push-based task transport, while Redis `QueueBackend` is pull-based. Select exactly
  one transport for a run.
- The Redis example client rejects every real I/O operation. It verifies composition only and is
  not a deployment template.

Credential loading belongs to the host application. Retrieve credentials from a configuration or
secrets service, construct external clients, inject them into MatterLoop, and close those clients
during application shutdown.
