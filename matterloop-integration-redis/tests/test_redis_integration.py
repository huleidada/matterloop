"""Redis 集成在无真实服务条件下的协议测试。"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping, Sequence
from dataclasses import fields, replace
from datetime import timedelta

import pytest
from matterloop_core import (
    CheckpointConflictError,
    CheckpointStore,
    LoopContext,
    LoopEvent,
    LoopEventType,
    LoopRequest,
    LoopStatus,
    StopReason,
    result_from_context,
)
from matterloop_integration_redis import (
    AsyncRedisClient,
    RedisCheckpointStore,
    RedisConfig,
    RedisEventPublisher,
    RedisPayloadCodec,
    RedisPayloadError,
    RedisQueueBackend,
    RedisRunRepository,
)
from matterloop_runtime import (
    DuplicateRunError,
    QueueAction,
    QueueBackend,
    QueuedRun,
    RunEventReader,
    RunRecord,
    RunRepository,
    RunStatus,
)


class FakeRedis:
    """只实现适配器测试需要的 Redis 命令语义。"""

    def __init__(self) -> None:
        self.strings: dict[str, object] = {}
        self.scores: dict[str, dict[str, float]] = {}
        self.queue_jobs: dict[str, str] = {}
        self.queue_attempts: dict[str, int] = {}
        self.pending: list[str] = []
        self.delayed: dict[str, float] = {}
        self.leases: dict[str, tuple[str, float]] = {}
        self.run_leases: dict[str, str] = {}
        self.cancelled: set[str] = set()
        self.streams: dict[str, list[tuple[str, Mapping[str, object]]]] = {}
        self.last_checkpoint_arguments: tuple[object, ...] | None = None
        self.closed = False

    async def eval(self, script: str, numkeys: int, *keys_and_args: object) -> object:
        keys = tuple(str(value) for value in keys_and_args[:numkeys])
        args = keys_and_args[numkeys:]
        if "matterloop:enqueue" in script:
            run_id, payload = str(args[0]), str(args[1])
            if run_id in self.queue_jobs:
                return 0
            self.queue_jobs[run_id] = payload
            self.queue_attempts[run_id] = 1
            self.cancelled.discard(run_id)
            self.pending.append(run_id)
            return 1
        if "matterloop:lease" in script:
            now, lease_id, expires_at = float(args[0]), str(args[1]), float(args[3])
            for run_id, available_at in tuple(self.delayed.items()):
                if available_at <= now:
                    del self.delayed[run_id]
                    self.pending.append(run_id)
            for expired_lease_id, (run_id, expiry) in tuple(self.leases.items()):
                if expiry > now:
                    continue
                del self.leases[expired_lease_id]
                if self.run_leases.get(run_id) == expired_lease_id:
                    del self.run_leases[run_id]
                if run_id in self.cancelled:
                    self._delete_queue_run(run_id)
                else:
                    self.queue_attempts[run_id] += 1
                    self.pending.insert(0, run_id)
            while self.pending:
                run_id = self.pending.pop(0)
                if run_id in self.cancelled:
                    self._delete_queue_run(run_id)
                    continue
                payload = self.queue_jobs.get(run_id)
                if payload is None:
                    continue
                self.leases[lease_id] = (run_id, expires_at)
                self.run_leases[run_id] = lease_id
                return [payload.encode(), str(self.queue_attempts[run_id]).encode()]
            return None
        if "matterloop:acknowledge" in script:
            lease_id, run_id = str(args[0]), str(args[1])
            current = self.leases.get(lease_id)
            if current is None or current[0] != run_id:
                return 0
            del self.leases[lease_id]
            self._delete_queue_run(run_id)
            return 1
        if "matterloop:release" in script:
            lease_id, run_id = str(args[0]), str(args[1])
            available_at, now = float(args[2]), float(args[3])
            current = self.leases.get(lease_id)
            if current is None or current[0] != run_id:
                return 0
            del self.leases[lease_id]
            self.run_leases.pop(run_id, None)
            if run_id in self.cancelled:
                self._delete_queue_run(run_id)
                return 1
            self.queue_attempts[run_id] += 1
            if available_at > now:
                self.delayed[run_id] = available_at
            else:
                self.pending.append(run_id)
            return 1
        if "matterloop:cancel" in script:
            run_id = str(args[0])
            if run_id not in self.queue_jobs:
                return 0
            self.cancelled.add(run_id)
            self.pending = [item for item in self.pending if item != run_id]
            self.delayed.pop(run_id, None)
            if run_id not in self.run_leases:
                self._delete_queue_run(run_id)
            return 1
        if "matterloop:checkpoint-save" in script:
            self.last_checkpoint_arguments = args
            checkpoint_key = keys[0]
            expected_revision, payload, run_id = int(args[0]), str(args[1]), str(args[2])
            try:
                replacement = json.loads(payload)
                replacement_context = replacement["context"]
                replacement_run_id = replacement_context["run_id"]
                replacement_revision = replacement_context["revision"]
            except (KeyError, TypeError, ValueError, json.JSONDecodeError):
                return -2
            if replacement_run_id != run_id or replacement_revision != expected_revision + 1:
                return -2

            current = self.strings.get(checkpoint_key)
            if current is None:
                if expected_revision != 0:
                    return 0
                self.strings[checkpoint_key] = payload
                return 1
            try:
                current_payload = current.decode("utf-8") if isinstance(current, bytes) else current
                decoded = json.loads(str(current_payload))
                current_context = decoded["context"]
                current_run_id = current_context["run_id"]
                current_revision = current_context["revision"]
            except (KeyError, TypeError, ValueError, UnicodeDecodeError, json.JSONDecodeError):
                return -2
            if current_run_id != run_id or not isinstance(current_revision, int):
                return -2
            if current_revision != expected_revision:
                return 0
            self.strings[checkpoint_key] = payload
            return expected_revision + 1
        if "matterloop:repository-create" in script:
            record_key, index_key = keys
            if record_key in self.strings:
                return 0
            self.strings[record_key] = args[0]
            self.scores.setdefault(index_key, {})[str(args[2])] = float(args[1])
            return 1
        if "matterloop:repository-cas" in script:
            record_key, index_key = keys
            current = self.strings.get(record_key)
            if current is None:
                return -1
            codec = RedisPayloadCodec()
            try:
                current_record = codec.loads_record(str(current))
                replacement_record = codec.loads_record(str(args[1]))
            except RedisPayloadError:
                return -2
            if current_record.run_id != str(args[3]) or replacement_record.run_id != str(args[3]):
                return -2
            if current_record.version != int(args[0]):
                return 0
            if replacement_record.version != int(args[0]) + 1:
                return -2
            if replacement_record.created_at != current_record.created_at:
                return -3
            self.strings[record_key] = args[1]
            self.scores.setdefault(index_key, {})[str(args[3])] = float(args[2])
            return 1
        raise AssertionError("unexpected Lua script")

    async def get(self, name: str) -> object:
        return self.strings.get(name)

    async def mget(self, keys: Sequence[str]) -> object:
        return [self.strings.get(key) for key in keys]

    async def zrevrange(self, name: str, start: int, end: int) -> object:
        members = sorted(
            self.scores.get(name, {}),
            key=lambda member: self.scores[name][member],
            reverse=True,
        )
        return [member.encode() for member in members[start : end + 1]]

    async def xadd(
        self,
        name: str,
        fields: Mapping[str, str],
        *,
        maxlen: int,
        approximate: bool,
    ) -> object:
        del approximate
        entries = self.streams.setdefault(name, [])
        identifier = f"{len(entries) + 1}-0"
        entries.append((identifier, dict(fields)))
        if len(entries) > maxlen:
            del entries[: len(entries) - maxlen]
        return identifier

    async def xrange(
        self,
        name: str,
        *,
        min: str,
        max: str,
        count: int,
    ) -> object:
        del max
        after = min[1:] if min.startswith("(") else None
        entries = self.streams.get(name, [])
        after_parts = None if after is None else tuple(int(part) for part in after.split("-"))
        return [
            (identifier.encode(), {key.encode(): value.encode() for key, value in fields.items()})
            for identifier, fields in entries
            if after_parts is None
            or tuple(int(part) for part in identifier.split("-")) > after_parts
        ][:count]

    async def aclose(self) -> None:
        self.closed = True

    def _delete_queue_run(self, run_id: str) -> None:
        self.queue_jobs.pop(run_id, None)
        self.queue_attempts.pop(run_id, None)
        self.run_leases.pop(run_id, None)
        self.delayed.pop(run_id, None)
        self.cancelled.discard(run_id)
        self.pending = [item for item in self.pending if item != run_id]


def test_config_contains_no_connection_or_environment_fields() -> None:
    """配置对象只能保存适配器行为，不得保存连接来源或凭据。"""
    config = RedisConfig()

    assert {item.name for item in fields(config)} == {
        "prefix",
        "lease_seconds",
        "event_max_length",
    }
    assert "env" not in repr(config).lower()
    assert "url" not in repr(config).lower()
    with pytest.raises(ValueError):
        RedisConfig(prefix="redis://user:secret@example.test/0")
    with pytest.raises(ValueError):
        RedisConfig(prefix="user:secret@example.test")


def test_adapters_keep_explicit_client_identity() -> None:
    """所有适配器都必须使用宿主应用显式注入的同一客户端。"""
    client = FakeRedis()

    queue = RedisQueueBackend(client)
    repository = RedisRunRepository(client)
    events = RedisEventPublisher(client)
    checkpoints = RedisCheckpointStore(client)

    assert isinstance(client, AsyncRedisClient)
    assert queue._client is client
    assert repository._client is client
    assert events._client is client
    assert checkpoints._client is client


def test_payload_codec_round_trips_jobs_and_results() -> None:
    """跨进程 DTO 必须保留请求边界、恢复动作和结构化结果。"""
    codec = RedisPayloadCodec()
    request = LoopRequest("完成 Redis 编解码", ("字段完整",))
    job = QueuedRun("run-1", QueueAction.START, request=request)

    restored_job = codec.loads_job(codec.dumps_job(job))

    assert restored_job.run_id == job.run_id
    assert restored_job.request == request
    context = LoopContext(request, run_id="run-1", status=LoopStatus.COMPLETED)
    context.stop_reason = StopReason.COMPLETED
    record = RunRecord(
        "run-1",
        request,
        status=RunStatus.COMPLETED,
        result=replace(result_from_context(context), output="done"),
    )

    restored_record = codec.loads_record(codec.dumps_record(record))

    assert restored_record == record


def test_checkpoint_serialization_errors_stay_inside_redis_error_boundary() -> None:
    """无法序列化的请求元数据不能泄露内核检查点异常。"""
    request = LoopRequest("非法元数据", metadata={"unsupported": object()})
    codec = RedisPayloadCodec()

    with pytest.raises(RedisPayloadError, match="checkpoint"):
        codec.dumps_job(QueuedRun("run-invalid", QueueAction.START, request=request))

    async def scenario() -> None:
        publisher = RedisEventPublisher(FakeRedis())
        context = LoopContext(request, run_id="run-invalid")
        with pytest.raises(RedisPayloadError, match="serializable"):
            await publisher.publish(LoopEvent(LoopEventType.LOOP_STARTED, context))

    asyncio.run(scenario())


def test_checkpoint_store_recovers_from_a_new_adapter_instance() -> None:
    """新建适配器实例应能从同一 Redis 恢复完整 Loop 检查点。"""

    async def scenario() -> None:
        client = FakeRedis()
        first_store = RedisCheckpointStore(client, RedisConfig(prefix="matterloop:{test}"))
        context = LoopContext(LoopRequest("跨实例恢复"), run_id="run-checkpoint")

        first_revision = await first_store.save(context)
        assert first_revision == 1
        assert context.revision == 0
        assert client.last_checkpoint_arguments is not None
        assert all(isinstance(argument, str) for argument in client.last_checkpoint_arguments)
        context.revision = first_revision
        context.status = LoopStatus.PLANNING
        context.cycle_count = 1
        context.feedback = "已生成恢复上下文"
        second_revision = await first_store.save(context)
        assert second_revision == 2

        restarted_store = RedisCheckpointStore(
            client,
            RedisConfig(prefix="matterloop:{test}"),
        )
        restored = await restarted_store.load(context.run_id)

        assert restored is not None
        assert restored.run_id == context.run_id
        assert restored.request == context.request
        assert restored.status is LoopStatus.PLANNING
        assert restored.cycle_count == 1
        assert restored.feedback == "已生成恢复上下文"
        assert restored.revision == second_revision

        key = "matterloop:{test}:checkpoints:run-checkpoint"
        stored = client.strings[key]
        assert isinstance(stored, str)
        client.strings[key] = stored.encode("utf-8")
        restored_from_bytes = await restarted_store.load(context.run_id)
        assert restored_from_bytes is not None
        assert restored_from_bytes.revision == second_revision

    asyncio.run(scenario())


def test_checkpoint_store_rejects_corrupted_redis_data() -> None:
    """损坏文本、非法 UTF-8 和错误类型都不得被当作可恢复检查点。"""

    async def scenario() -> None:
        client = FakeRedis()
        store = RedisCheckpointStore(client)
        key = "matterloop:checkpoints:run-corrupted"

        client.strings[key] = "{not-json"
        with pytest.raises(RedisPayloadError, match="invalid"):
            await store.load("run-corrupted")

        client.strings[key] = b"\xff"
        with pytest.raises(RedisPayloadError, match="UTF-8"):
            await store.load("run-corrupted")

        client.strings[key] = 42
        with pytest.raises(RedisPayloadError, match="text or bytes"):
            await store.load("run-corrupted")

        client.strings[key] = "{still-not-json"
        context = LoopContext(LoopRequest("拒绝覆盖损坏数据"), run_id="run-corrupted")
        with pytest.raises(RedisPayloadError, match="corrupted"):
            await store.save(context)

    asyncio.run(scenario())


def test_checkpoint_store_validates_run_id_and_expected_revision() -> None:
    """Key 标识与 CAS revision 必须在访问 Redis 前通过边界校验。"""

    async def scenario() -> None:
        store = RedisCheckpointStore(FakeRedis())

        with pytest.raises(ValueError, match="run_id"):
            await store.save(LoopContext(LoopRequest("空标识"), run_id=" "))
        with pytest.raises(ValueError, match="expected_revision"):
            await store.save(
                LoopContext(LoopRequest("非法版本"), run_id="run-invalid-revision"),
                expected_revision=True,
            )
        with pytest.raises(ValueError, match="run_id"):
            await store.load(" ")

    asyncio.run(scenario())


def test_checkpoint_store_rejects_stale_revision_without_overwriting_winner() -> None:
    """两个实例同时更新时，陈旧 revision 必须冲突且不能覆盖胜者。"""

    async def scenario() -> None:
        client = FakeRedis()
        first_store = RedisCheckpointStore(client)
        second_store = RedisCheckpointStore(client)
        initial = LoopContext(LoopRequest("检查点 CAS"), run_id="run-conflict")
        initial.revision = await first_store.save(initial)

        first_copy = await first_store.load(initial.run_id)
        stale_copy = await second_store.load(initial.run_id)
        assert first_copy is not None and stale_copy is not None
        first_copy.feedback = "winner"
        winning_revision = await first_store.save(first_copy)
        assert winning_revision == 2

        stale_copy.feedback = "stale"
        with pytest.raises(CheckpointConflictError, match="expected 1"):
            await second_store.save(stale_copy)

        restored = await second_store.load(initial.run_id)
        assert restored is not None
        assert restored.feedback == "winner"
        assert restored.revision == winning_revision

        missing = LoopContext(LoopRequest("不存在"), run_id="missing", revision=3)
        with pytest.raises(CheckpointConflictError, match="expected 3"):
            await second_store.save(missing)

    asyncio.run(scenario())


def test_queue_backend_exposes_enqueue_lease_ack_and_cancel() -> None:
    """Redis 队列适配器应满足完整租约协议。"""

    async def scenario() -> None:
        client = FakeRedis()
        backend = RedisQueueBackend(client)
        job = QueuedRun("run-queue", QueueAction.START, request=LoopRequest("排队"))
        await backend.enqueue(job)
        with pytest.raises(DuplicateRunError):
            await backend.enqueue(job)

        lease = await backend.lease("worker-1", 30)
        assert lease is not None
        assert lease.job == job
        assert lease.attempt == 1
        await backend.release(lease, delay_seconds=1)
        await backend.acknowledge(lease)
        assert not await backend.cancel("missing")

    asyncio.run(scenario())


def test_queue_release_expiry_and_cancel_preserve_atomic_state() -> None:
    """释放、过期回收和租约中取消必须准确增加尝试次数并清理状态。"""

    async def scenario() -> None:
        client = FakeRedis()
        backend = RedisQueueBackend(client)
        job = QueuedRun("run-state", QueueAction.START, request=LoopRequest("状态机"))
        await backend.enqueue(job)

        first = await backend.lease("worker-1", 0.001)
        assert first is not None
        await asyncio.sleep(0.01)
        recovered = await backend.lease("worker-2", 30)
        assert recovered is not None
        assert recovered.attempt == 2
        # 过期工作进程的迟到确认不能删除新租约。
        await backend.acknowledge(first)
        assert "run-state" in client.queue_jobs

        assert await backend.cancel("run-state")
        await backend.release(recovered)
        assert not client.queue_jobs
        assert not client.cancelled
        assert await backend.lease("worker-3", 30) is None

        # 完全清理后允许相同 run_id 用于合法恢复命令。
        await backend.enqueue(job)
        leased_again = await backend.lease("worker-3", 30)
        assert leased_again is not None and leased_again.attempt == 1

    asyncio.run(scenario())


def test_delayed_release_is_not_visible_before_due_time() -> None:
    """延迟释放不能被其他工作进程提前租用。"""

    async def scenario() -> None:
        client = FakeRedis()
        backend = RedisQueueBackend(client)
        job = QueuedRun("run-delay", QueueAction.START, request=LoopRequest("延迟"))
        await backend.enqueue(job)
        lease = await backend.lease("worker-1", 30)
        assert lease is not None
        await backend.release(lease, delay_seconds=0.02)
        assert await backend.lease("worker-2", 30) is None
        await asyncio.sleep(0.03)
        retried = await backend.lease("worker-2", 30)
        assert retried is not None and retried.attempt == 2

    asyncio.run(scenario())


def test_run_repository_supports_atomic_version_updates_and_paging() -> None:
    """运行记录应拒绝重复创建并通过版本执行 CAS。"""

    async def scenario() -> None:
        client = FakeRedis()
        repository = RedisRunRepository(client)
        record = RunRecord("run-repository", LoopRequest("保存运行"))
        await repository.create(record)
        with pytest.raises(DuplicateRunError):
            await repository.create(record)
        assert await repository.get(record.run_id) == record
        assert await repository.list() == (record,)

        replacement = replace(record, status=RunStatus.RUNNING, version=1)
        stale_replacement = replace(replacement, version=100)
        assert not await repository.compare_and_set(
            record.run_id,
            99,
            stale_replacement,
        )
        assert await repository.compare_and_set(record.run_id, 0, replacement)
        assert await repository.get(record.run_id) == replacement

    asyncio.run(scenario())


def test_run_repository_rejects_corruption_and_creation_time_changes() -> None:
    """CAS 应把损坏记录和不可变创建时间变更转换为类型化错误。"""

    async def scenario() -> None:
        client = FakeRedis()
        repository = RedisRunRepository(client)
        record = RunRecord("run-cas", LoopRequest("CAS"))
        await repository.create(record)
        changed_time = replace(
            record,
            version=1,
            created_at=record.created_at + timedelta(seconds=1),
        )
        with pytest.raises(RedisPayloadError, match="creation time"):
            await repository.compare_and_set(record.run_id, 0, changed_time)

        client.strings["matterloop:runs:run-cas"] = "{invalid-json"
        replacement = replace(record, version=1)
        with pytest.raises(RedisPayloadError, match="corrupted"):
            await repository.compare_and_set(record.run_id, 0, replacement)

    asyncio.run(scenario())


def test_event_publisher_also_reads_paginated_stream_events() -> None:
    """发布器写入的事件应可直接交给 QueueRuntime 的事件读取入口。"""

    async def scenario() -> None:
        client = FakeRedis()
        publisher = RedisEventPublisher(client, RedisConfig(event_max_length=10))
        context = LoopContext(LoopRequest("审计事件"), run_id="run-events")
        await publisher.publish(LoopEvent(LoopEventType.LOOP_STARTED, context))

        events = await publisher.list_events(context.run_id)

        assert len(events) == 1
        assert events[0]["event_id"] == "1-0"
        assert events[0]["event_type"] == LoopEventType.LOOP_STARTED.value
        assert await publisher.list_events(context.run_id, after="1-0") == ()

    asyncio.run(scenario())


def test_event_reader_validates_cursor_identity_and_payload() -> None:
    """事件游标不能被载荷覆盖，跨运行载荷和非法游标必须被拒绝。"""

    async def scenario() -> None:
        client = FakeRedis()
        publisher = RedisEventPublisher(client)
        context = LoopContext(LoopRequest("事件验证"), run_id="run-events")
        await publisher.publish(LoopEvent(LoopEventType.LOOP_STARTED, context))
        key = "matterloop:events:run-events"
        identifier, fields = client.streams[key][0]
        assert isinstance(fields, dict)
        payload = json.loads(str(fields["payload"]))
        payload["event_id"] = "forged-id"
        fields["payload"] = json.dumps(payload)

        events = await publisher.list_events("run-events")
        assert events[0]["event_id"] == identifier
        with pytest.raises(ValueError, match="Stream ID"):
            await publisher.list_events("run-events", after="not-a-cursor")

        payload["run_id"] = "other-run"
        fields["payload"] = json.dumps(payload)
        with pytest.raises(RedisPayloadError, match="run_id"):
            await publisher.list_events("run-events")

    asyncio.run(scenario())


def test_adapters_satisfy_runtime_protocols() -> None:
    """Redis 适配器应结构化满足 runtime 扩展协议。"""
    client = FakeRedis()

    assert isinstance(RedisQueueBackend(client), QueueBackend)
    assert isinstance(RedisRunRepository(client), RunRepository)
    assert isinstance(RedisEventPublisher(client), RunEventReader)
    assert isinstance(RedisCheckpointStore(client), CheckpointStore)
