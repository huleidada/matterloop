[简体中文](https://github.com/huleidada/matterloop/blob/main/matterloop-integration-redis/README.md) | English

# matterloop-integration-redis

This package provides persistent Loop checkpoints, task queues, run records, and event streams for
MatterLoop. It does not turn Redis into a universal backend: the application still selects and
assembles long-term memory and Workers.

```bash
pip install matterloop-integration-redis
```

## Four adapters with separate responsibilities

| Adapter | Data stored | Key guarantee |
| --- | --- | --- |
| `RedisCheckpointStore` | Complete `LoopContext` checkpoints | Revision CAS; strict Core checkpoint schema |
| `RedisQueueBackend` | Pending jobs, delayed jobs, leases, and cancellation markers | Atomic single-node state transitions in Lua; at-least-once delivery |
| `RedisRunRepository` | `RunRecord` and creation-time index | Version CAS prevents concurrent overwrite |
| `RedisEventPublisher` | One Core event Stream per run | Ordered cursor reads; approximate length trimming |

`RedisCheckpointStore` stores the plan, step cursor, human feedback, pending execution awaiting
verification, and revision required for exact recovery. `RedisRunRepository` stores status and
results queried by the control plane. The two data sets serve different purposes and are not
interchangeable.

## Assembly

```python
from matterloop_integration_redis import (
    RedisCheckpointStore,
    RedisConfig,
    RedisEventPublisher,
    RedisQueueBackend,
    RedisRunRepository,
)

config = RedisConfig(prefix="matterloop:{prod}", lease_seconds=300)
checkpoints = RedisCheckpointStore(client=redis_client, config=config, codec=None)
queue = RedisQueueBackend(client=redis_client, config=config, codec=None)
runs = RedisRunRepository(client=redis_client, config=config, codec=None)
events = RedisEventPublisher(
    client=redis_client,
    config=config,
    checkpoint_codec=None,
)
```

The application creates `redis_client` and configures TLS, authentication, timeouts, and connection
pooling. All four adapters may share the client, but none of them closes it.

`RedisConfig(prefix, lease_seconds, event_max_length)` stores non-sensitive behavior configuration
only. The defaults are `"matterloop"`, `60.0` seconds, and an approximate `10_000` events,
respectively. Connection URLs, usernames, and passwords do not belong in this configuration
object.

## Checkpoint fields and CAS semantics

| API / field | Type | Required | Default | Business meaning | Validation and persistence |
| --- | --- | --- | --- | --- | --- |
| `RedisCheckpointStore.client` | `AsyncRedisClient` | Yes | None | Executes `GET` and Lua CAS | The host owns the connection; it is not stored in the payload |
| `RedisCheckpointStore.config` | `Optional[RedisConfig]` | No | `RedisConfig()` | Supplies the Key prefix | Stores neither connection data nor credentials |
| `RedisCheckpointStore.codec` | `Optional[LoopCheckpointCodec]` | No | `LoopCheckpointCodec()` | Encodes and decodes Core's current checkpoint layout | Invalid fields fail strictly |
| `save.context` | `LoopContext` | Yes | None | Complete isolated snapshot to commit | Stored as a JSON String at `<prefix>:checkpoints:<run_id>` |
| `save.expected_revision` | `Optional[int]` | No | `context.revision` | CAS version observed by the caller | Must be a non-negative integer; create requires `0` |
| `load.run_id` | `str` | Yes | None | Stable run identifier to recover | Empty values fail; payload `run_id` must match the Key |

`save()` reads the current value, validates its schema and `run_id`, compares the revision, and
writes the new snapshot in one Lua script. It returns `expected_revision + 1` on success but does
not mutate the caller's `context.revision`; the controller must adopt the returned value. A version
mismatch raises `CheckpointConflictError`. Corrupt JSON, invalid UTF-8, an unknown checkpoint
schema, or an invalid Redis response raises `RedisPayloadError`. `load()` returns `None` when the Key
does not exist.

A Redis command timeout has an unknown outcome: Lua may already have committed. The caller must
`load()` and inspect the revision before replaying work or side effects. Checkpoints have no TTL,
listing API, or deletion API, and do not form a cross-Key transaction with `RunRecord` or the event
Stream. Retention, cleanup, and an Outbox are deployment responsibilities.

## The queue is not "exactly once"

A Worker calls `lease()` to acquire a job, `acknowledge()` after success, and `release()` after a
retryable failure. An expired lease is recovered only by the next `lease()` call. The current
interface has no lease renewal, maximum-attempt limit, or dead-letter queue. If execution may exceed
the lease, either increase the lease with clock-skew headroom or divide the job into smaller units.

When a network timeout occurs, Lua may already have committed. External writes must carry an
idempotency key, and final run state must be committed through `RunRepository.compare_and_set()`.
`cancel()` can prevent a waiting job from running or mark a leased job as cancelled, but it cannot
interrupt Python code that is already executing.

## Key layout

Every Key is stored under `<prefix>`:

```text
<prefix>:queue:pending          List
<prefix>:queue:delayed          Sorted Set
<prefix>:queue:jobs             Hash
<prefix>:queue:leases           Hash
<prefix>:queue:lease-expiry     Sorted Set
<prefix>:checkpoints:<run_id>   String (Core current checkpoint layout, revision CAS)
<prefix>:runs:index             Sorted Set
<prefix>:runs:<run_id>          String (versioned JSON)
<prefix>:events:<run_id>        Stream
```

Checkpoints, run records, and events have no TTL or deletion API. Archival, deletion, capacity
alerts, and data retention are deployment responsibilities.

## Redis Cluster

Queue scripts, repository CAS, and batch reads access multiple Keys, so those Keys must occupy the
same hash slot; checkpoint CAS accesses only its own Key. The default prefix can produce
`CROSSSLOT` in a Cluster. Use a fixed hash tag such as `matterloop:{prod}`. To shard horizontally,
configure separate prefixes and clients per tenant or implement another backend.

## Events and sensitive data

`RedisCheckpointStore` and `RedisEventPublisher` may store goals, model output, metadata, tool
results, and human feedback. This package does not invoke `Redactor` automatically. Production
deployments should configure Redis ACLs, TLS, encryption at rest, retention periods, tenant
isolation, and payload size limits.

Event reads use the exclusive Stream cursor `after`. If the Stream has been trimmed, an old cursor
does not report a gap; it resumes with the next entry that still exists. When audit records must not
be lost, do not use an approximately trimmed Redis Stream as the sole system of record.

## Protocols and errors

`AsyncRedisClient` is a minimal structural protocol requiring `eval`, `get`, `mget`, `zrevrange`,
`xadd`, `xrange`, and `aclose`. Adapters only await those asynchronous methods and do not read
connection configuration. `RedisPayloadCodec` and `LoopCheckpointCodec` use strict, versioned JSON.
An unknown version or corrupt payload raises `RedisPayloadError`. Network, ACL, timeout, and Cluster
errors propagate unchanged from the underlying client; this package does not retry them
automatically.

Construction entry points are `RedisCheckpointStore(client, config, codec)`,
`RedisQueueBackend(client, config, codec)`, `RedisRunRepository(client, config, codec)`, and
`RedisEventPublisher(client, config, checkpoint_codec)`. See the
[Enterprise Integration Guide](../docs/enterprise-integration.en.md) for the complete queue control
plane and shutdown order.
