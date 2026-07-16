[简体中文](https://github.com/huleidada/matterloop/blob/main/matterloop-integration-redis/README.md) | English

# matterloop-integration-redis

This package connects Redis to MatterLoop queues, run records, and event streams. It does not turn
Redis into a universal backend: long-term memory, Loop checkpoints, and Workers are outside its
scope.

```bash
pip install matterloop-integration-redis
```

## Three adapters with separate responsibilities

| Adapter | Data stored | Key guarantee |
| --- | --- | --- |
| `RedisQueueBackend` | Pending jobs, delayed jobs, leases, and cancellation markers | Atomic single-node state transitions in Lua; at-least-once delivery |
| `RedisRunRepository` | `RunRecord` and creation-time index | Version CAS prevents concurrent overwrite |
| `RedisEventPublisher` | One Core event Stream per run | Ordered cursor reads; approximate length trimming |

`RedisRunRepository` is not a `CheckpointStore`. Exact recovery requires the plan, current step,
human feedback, and revision to be stored by a separate persistent implementation.

## Assembly

```python
from matterloop_integration_redis import (
    RedisConfig,
    RedisEventPublisher,
    RedisQueueBackend,
    RedisRunRepository,
)

config = RedisConfig(prefix="matterloop:{prod}", lease_seconds=300)
queue = RedisQueueBackend(client=redis_client, config=config, codec=None)
runs = RedisRunRepository(client=redis_client, config=config, codec=None)
events = RedisEventPublisher(
    client=redis_client,
    config=config,
    checkpoint_codec=None,
)
```

The application creates `redis_client` and configures TLS, authentication, timeouts, and connection
pooling. All three adapters may share the client, but none of them closes it.

`RedisConfig(prefix, lease_seconds, event_max_length)` stores non-sensitive behavior configuration
only. The defaults are `"matterloop"`, `60.0` seconds, and an approximate `10_000` events,
respectively. Connection URLs, usernames, and passwords do not belong in this configuration
object.

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
<prefix>:runs:index             Sorted Set
<prefix>:runs:<run_id>          String (versioned JSON)
<prefix>:events:<run_id>        Stream
```

Run records and events have no TTL or deletion API. Archival, deletion, capacity alerts, and data
retention are deployment responsibilities.

## Redis Cluster

Queue scripts, repository CAS, and batch reads access multiple Keys, so those Keys must occupy the
same hash slot. The default prefix can produce `CROSSSLOT` in a Cluster. Use a fixed hash tag such as
`matterloop:{prod}`. This concentrates the data set in one slot. To shard horizontally, configure
separate prefixes and clients per tenant or implement another backend.

## Events and sensitive data

`RedisEventPublisher` writes the complete checkpoint at event time into the Stream. It may include
the goal, model output, metadata, and human feedback. This package does not invoke `Redactor`
automatically. Production deployments should configure Redis ACLs, TLS, encryption at rest,
retention periods, and per-event size limits.

Event reads use the exclusive Stream cursor `after`. If the Stream has been trimmed, an old cursor
does not report a gap; it resumes with the next entry that still exists. When audit records must not
be lost, do not use an approximately trimmed Redis Stream as the sole system of record.

## Protocols and errors

`AsyncRedisClient` is a minimal structural protocol requiring `eval`, `get`, `mget`, `zrevrange`,
`xadd`, `xrange`, and `aclose`. `RedisPayloadCodec` uses strict, versioned JSON. An unknown version
or corrupt payload raises `RedisPayloadError`. Network, ACL, timeout, and Cluster errors propagate
unchanged from the underlying client; this package does not retry them automatically.

Construction entry points are `RedisQueueBackend(client, config, codec)`,
`RedisRunRepository(client, config, codec)`, and
`RedisEventPublisher(client, config, checkpoint_codec)`. See the
[Enterprise Integration Guide](../docs/enterprise-integration.en.md) for the complete queue control
plane and shutdown order.
