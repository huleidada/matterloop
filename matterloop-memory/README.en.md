[简体中文](https://github.com/huleidada/matterloop/blob/main/matterloop-memory/README.md) | English

# matterloop-memory

`matterloop-memory` handles two concerns that are easy to conflate: retrieving historical information for an Agent
and storing Loop recovery points. It provides protocols and in-process implementations only; it does not choose a
database for you.

```bash
pip install matterloop-memory
```

## Separate the two data categories first

| Data | Interface | Purpose | Part of task recovery |
| --- | --- | --- | --- |
| Long-term memory | `MemoryStore` | Store facts, experiences, and operating rules for Agent retrieval | No |
| Loop checkpoint | `CheckpointStore` | Store the state machine, current step, human feedback, and revision | Yes |

These categories have different retention, authorization, and consistency requirements. Do not put them into one
generic “memory table” in production.

## Minimal usage

```python
from matterloop_memory import (
    InMemoryCheckpointStore,
    InMemoryMemoryStore,
    MemoryKind,
    MemoryQuery,
    MemoryRecord,
)

memory = InMemoryMemoryStore()
checkpoints = InMemoryCheckpointStore()

await memory.put(
    MemoryRecord(
        namespace="tenant/acme/project/docs",
        kind=MemoryKind.SEMANTIC,
        content="An independent verifier must accept the result before release",
        metadata={"source": "engineering-policy"},
    )
)
matches = await memory.search(
    MemoryQuery(namespace="tenant/acme/project/docs", text="release verification", limit=5)
)
```

Use `NullMemoryStore` to disable long-term memory explicitly; it is easier to test than propagating `None` through
business code. `InMemoryCheckpointStore` can be injected directly into `AgentLoop(checkpoint_store=...)`.

## Runtime semantics

- `namespace` is a query condition, not an authorization mechanism. Derive it from trusted identity rather than
  copying client input.
- `InMemoryMemoryStore` uses a simple term-intersection score, not vector retrieval. Its `score` must not be compared
  directly with scores from other backends.
- Expired records are hidden when read but are not reclaimed in the background. Long-running services need an
  implementation with a cleanup policy.
- `InMemoryCheckpointStore.save()` uses revision CAS. A conflict means another writer has advanced the state; reload
  it instead of overwriting it.
- Every in-memory implementation guarantees consistency only within one process and loses data when the process exits.

## Integrate a persistent backend

A custom long-term memory backend only needs to implement the structural `MemoryStore` protocol:

```python
class MemoryStore(Protocol):
    async def put(self, record: MemoryRecord) -> None: ...
    async def get(self, record_id: str) -> MemoryRecord | None: ...
    async def search(self, query: MemoryQuery) -> tuple[MemoryMatch, ...]: ...
    async def delete(self, record_id: str) -> bool: ...
    async def clear(self, namespace: str) -> int: ...
```

Persistent checkpoints implement `matterloop_core.CheckpointStore`. At minimum, provide atomic CAS, tenant isolation,
encryption, backups, and auditable deletion. Do not substitute a long-term-memory similarity index for state storage.

<details>
<summary>Public data structure reference</summary>

- `MemoryKind`: `SEMANTIC`, `EPISODIC`, `PROCEDURAL`.
- `MemoryRecord(namespace, kind, content, metadata, record_id, created_at, expires_at)`: one complete memory record;
  `record_id` generates a UUID by default, `created_at` uses the current UTC time by default, and `expires_at=None`
  means no expiry.
- `MemoryQuery(namespace, text, kinds, limit, min_score, filters)`: retrieval conditions; `limit=10`, `min_score=0`,
  and empty `kinds` or `filters` add no corresponding filter.
- `MemoryMatch(record, score)`: a matching record and the relevance reported by its backend.
- `InMemoryMemoryStore`, `NullMemoryStore`, `InMemoryCheckpointStore`: the three implementations provided by this
  package.

`metadata`, `content`, and filter values are not redacted automatically. Custom backends should limit record size,
filterable fields, and query cost.

</details>

## Out of scope

This package does not provide PostgreSQL, Redis, a vector database, Embeddings, cross-process locks, or background TTL
cleanup. See the [Enterprise integration guide](../docs/enterprise-integration.en.md) for deployment and data
governance guidance, and the [Architecture guide](../docs/architecture.en.md) for Loop recovery semantics.
