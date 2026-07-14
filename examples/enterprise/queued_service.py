"""演示 FastAPI、拉取式队列、Celery 和 Redis 的企业组合根边界。"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace

from fastapi import FastAPI
from matterloop_core import CheckpointStore, EventPublisher, LoopEvent, LoopRequest, LoopStatus
from matterloop_integration_celery import (
    CeleryApp,
    CeleryQueueProducer,
    CeleryTaskFunction,
    RegisteredCeleryTasks,
    register_tasks,
)
from matterloop_integration_fastapi import create_router
from matterloop_integration_redis import (
    AsyncRedisClient,
    RedisConfig,
    RedisEventPublisher,
    RedisQueueBackend,
    RedisRunRepository,
)
from matterloop_memory import InMemoryCheckpointStore
from matterloop_models import FakeModelClient, ModelClient, ModelResponse, TokenUsage
from matterloop_presets import ProductionRuntime, build_production_runtime
from matterloop_runtime import (
    InMemoryQueueBackend,
    InMemoryRunRepository,
    QueueBackend,
    QueueRuntime,
    RunEventReader,
    RunRepository,
    RunStatus,
)

logger = logging.getLogger(__name__)


async def allow_offline_request() -> str:
    """为离线示例返回固定主体；生产必须替换为真实鉴权依赖。

    Returns:
        不含凭据的示例主体标识。
    """
    return "offline-enterprise-user"


class RecordingAuditPublisher:
    """同时实现审计写入和只读分页的内存测试替身。"""

    def __init__(self) -> None:
        self._events: dict[str, list[dict[str, object]]] = {}

    async def publish(self, event: LoopEvent) -> None:
        """只保存路由展示所需的非敏感事件字段。

        Args:
            event: Core 产生的生命周期事件。
        """
        self._events.setdefault(event.context.run_id, []).append(
            {
                "id": str(event.sequence),
                "event": event.event_type.value,
                "status": event.context.status.value,
                "occurred_at": event.occurred_at,
            }
        )

    async def list_events(
        self,
        run_id: str,
        *,
        after: str | None = None,
        limit: int = 100,
    ) -> tuple[Mapping[str, object], ...]:
        """按事件序号读取审计记录。

        Args:
            run_id: 运行标识。
            after: 可选的上一事件序号。
            limit: 最大返回数量。

        Returns:
            事件映射的不可变分页。
        """
        start = 0 if after is None else int(after)
        return tuple(self._events.get(run_id, ())[start : start + limit])


@dataclass(frozen=True, slots=True)
class PullServiceDependencies:
    """拉取式生产组合根需要显式提供的全部资源。"""

    model: ModelClient
    queue_backend: QueueBackend
    run_repository: RunRepository
    checkpoint_store: CheckpointStore
    audit_publisher: EventPublisher
    auth_dependency: Callable[..., object]
    event_reader: RunEventReader | None = None


@dataclass(frozen=True, slots=True)
class PullService:
    """FastAPI 控制面与生产 worker runtime 的组合结果。"""

    app: FastAPI
    runtime: ProductionRuntime
    route_paths: tuple[str, ...]


def build_pull_service(dependencies: PullServiceDependencies) -> PullService:
    """构建使用完整 QueueBackend 的拉取式服务。

    Args:
        dependencies: 调用方已经构造并负责生命周期的企业资源。

    Returns:
        可挂载的 FastAPI 应用和分离的控制面/worker runtime。
    """
    runtime = build_production_runtime(
        dependencies.model,
        queue_backend=dependencies.queue_backend,
        run_repository=dependencies.run_repository,
        checkpoint_store=dependencies.checkpoint_store,
        audit_publisher=dependencies.audit_publisher,
        event_reader=dependencies.event_reader,
    )
    router = create_router(runtime, dependencies.auth_dependency)
    route_paths = _route_paths(router.routes)
    app = FastAPI(title="MatterLoop pull-worker example")
    app.include_router(router)
    return PullService(app=app, runtime=runtime, route_paths=route_paths)


@dataclass(frozen=True, slots=True)
class CeleryService:
    """Celery 推送式控制面和已注册 Worker 任务。"""

    app: FastAPI
    runtime: QueueRuntime
    tasks: RegisteredCeleryTasks


def build_celery_service(
    celery_app: CeleryApp,
    repository: RunRepository,
    *,
    runtime_factory_path: str,
    auth_dependency: Callable[..., object],
    queue: str | None = None,
) -> CeleryService:
    """构建 Celery 推送式控制面并注册 DTO Worker 任务。

    Args:
        celery_app: 宿主创建的 Celery 应用。
        repository: API 与 Worker 共享且支持 CAS 的运行仓储。
        runtime_factory_path: Worker 可导入的 `模块:无参工厂` 路径。
        auth_dependency: 应用于全部 MatterLoop 路由的鉴权依赖。
        queue: 可选 Celery 目标队列。

    Returns:
        使用 `CeleryQueueProducer` 的控制面和任务注册结果。
    """
    producer = CeleryQueueProducer(celery_app, queue=queue)
    runtime = QueueRuntime(producer, repository)
    app = FastAPI(title="MatterLoop Celery example")
    app.include_router(create_router(runtime, auth_dependency))
    tasks = register_tasks(celery_app, runtime_factory_path)
    return CeleryService(app=app, runtime=runtime, tasks=tasks)


@dataclass(frozen=True, slots=True)
class RedisService:
    """Redis 拉取式控制面及其三个独立适配器。"""

    app: FastAPI
    runtime: QueueRuntime
    queue_backend: RedisQueueBackend
    repository: RedisRunRepository
    events: RedisEventPublisher


def build_redis_service(
    client: AsyncRedisClient,
    *,
    auth_dependency: Callable[..., object],
    config: RedisConfig | None = None,
) -> RedisService:
    """用同一个外部 Redis client 构造队列、仓储和事件适配器。

    Args:
        client: 宿主创建并负责关闭的异步 Redis 客户端。
        auth_dependency: FastAPI 鉴权依赖。
        config: 不包含连接信息的 Redis Key/lease/Stream 配置。

    Returns:
        Redis 控制面。该对象不包含 CheckpointStore 或 Worker。
    """
    actual_config = config or RedisConfig(prefix="{matterloop}:enterprise-example")
    queue_backend = RedisQueueBackend(client, config=actual_config)
    repository = RedisRunRepository(client, config=actual_config)
    events = RedisEventPublisher(client, config=actual_config)
    runtime = QueueRuntime(queue_backend, repository, event_reader=events)
    app = FastAPI(title="MatterLoop Redis example")
    app.include_router(create_router(runtime, auth_dependency))
    return RedisService(
        app=app,
        runtime=runtime,
        queue_backend=queue_backend,
        repository=repository,
        events=events,
    )


class RecordingCeleryControl:
    """记录离线示例中的非终止撤销请求。"""

    def __init__(self) -> None:
        self.revoked: list[tuple[str, bool]] = []

    def revoke(self, task_id: str, *, terminate: bool = False) -> object:
        """记录确定性任务标识和终止选项。"""
        self.revoked.append((task_id, terminate))
        return None


class RecordingCeleryApp:
    """不连接 Broker 的 Celery 最小协议测试替身。"""

    def __init__(self) -> None:
        self.control = RecordingCeleryControl()
        self.sent: list[tuple[str, Mapping[str, object] | None, Mapping[str, object]]] = []
        self.registered: dict[str, CeleryTaskFunction] = {}

    def send_task(
        self,
        name: str,
        args: tuple[object, ...] | None = None,
        kwargs: Mapping[str, object] | None = None,
        **options: object,
    ) -> object:
        """记录序列化任务，不执行网络 I/O。"""
        if args is not None:
            raise ValueError("enterprise example only accepts keyword task payloads")
        self.sent.append((name, kwargs, dict(options)))
        return object()

    def task(
        self,
        **options: object,
    ) -> Callable[[CeleryTaskFunction], CeleryTaskFunction]:
        """记录任务注册选项并返回原函数。"""

        def decorator(function: CeleryTaskFunction) -> CeleryTaskFunction:
            name = options.get("name")
            if not isinstance(name, str):
                raise ValueError("Celery task requires a name")
            self.registered[name] = function
            return function

        return decorator


class NoIoRedisClient:
    """只用于验证 Redis 组合关系、禁止意外 I/O 的协议替身。"""

    def __init__(self) -> None:
        self.closed = False

    async def eval(self, script: str, numkeys: int, *keys_and_args: object) -> object:
        """禁止示例误执行 Redis Lua。"""
        del script, numkeys, keys_and_args
        raise AssertionError("offline wiring example must not execute Redis commands")

    async def get(self, name: str) -> object:
        """禁止示例误读 Redis String。"""
        del name
        raise AssertionError("offline wiring example must not execute Redis commands")

    async def mget(self, keys: Sequence[str]) -> object:
        """禁止示例误批量读取 Redis String。"""
        del keys
        raise AssertionError("offline wiring example must not execute Redis commands")

    async def zrevrange(self, name: str, start: int, end: int) -> object:
        """禁止示例误读取 Redis Sorted Set。"""
        del name, start, end
        raise AssertionError("offline wiring example must not execute Redis commands")

    async def xadd(
        self,
        name: str,
        fields: Mapping[str, str],
        *,
        maxlen: int,
        approximate: bool,
    ) -> object:
        """禁止示例误写 Redis Stream。"""
        del name, fields, maxlen, approximate
        raise AssertionError("offline wiring example must not execute Redis commands")

    async def xrange(
        self,
        name: str,
        *,
        min: str,
        max: str,
        count: int,
    ) -> object:
        """禁止示例误读 Redis Stream。"""
        del name, min, max, count
        raise AssertionError("offline wiring example must not execute Redis commands")

    async def aclose(self) -> None:
        """模拟由宿主显式关闭共享 Redis client。"""
        self.closed = True


@dataclass(frozen=True, slots=True)
class QueuedExampleResult:
    """队列服务示例的可断言结果摘要。"""

    run_id: str
    status: RunStatus
    event_count: int
    api_routes: tuple[str, ...]
    celery_task_name: str
    celery_registered_tasks: tuple[str, ...]
    redis_components: tuple[str, ...]
    redis_client_closed: bool


async def run_queued_example() -> QueuedExampleResult:
    """离线执行拉取式完整流程并验证 Celery/Redis 互斥接线。

    Returns:
        运行状态、审计数量和两类外部队列的结构摘要。
    """
    queue_backend = InMemoryQueueBackend()
    repository = InMemoryRunRepository()
    checkpoint_store = InMemoryCheckpointStore()
    audit = RecordingAuditPublisher()
    pull_service = build_pull_service(
        PullServiceDependencies(
            model=_build_worker_model(),
            queue_backend=queue_backend,
            run_repository=repository,
            checkpoint_store=checkpoint_store,
            audit_publisher=audit,
            auth_dependency=allow_offline_request,
            event_reader=audit,
        )
    )

    async with pull_service.runtime:
        run_id = await pull_service.runtime.submit(
            LoopRequest("离线执行企业队列任务", ("独立验证通过",)),
            run_id="enterprise-queue-example",
        )
        await _process_one_pull_job(
            pull_service.runtime,
            queue_backend,
            repository,
        )
        record = await pull_service.runtime.get(run_id)
        events = await pull_service.runtime.list_events(run_id)
    if record is None:
        raise RuntimeError("queue example lost its run record")

    celery_app = RecordingCeleryApp()
    celery_service = build_celery_service(
        celery_app,
        InMemoryRunRepository(),
        runtime_factory_path="application.workers:create_matterloop_dependencies",
        auth_dependency=allow_offline_request,
        queue="matterloop",
    )
    celery_run_id = await celery_service.runtime.submit(
        LoopRequest("投递 Celery DTO"),
        run_id="enterprise-celery-example",
    )
    if not celery_app.sent:
        raise RuntimeError("Celery example did not enqueue a task")
    celery_task_name = celery_app.sent[0][0]

    redis_client = NoIoRedisClient()
    redis_service = build_redis_service(
        redis_client,
        auth_dependency=allow_offline_request,
    )
    await redis_client.aclose()

    return QueuedExampleResult(
        run_id=run_id,
        status=record.status,
        event_count=len(events),
        api_routes=pull_service.route_paths,
        celery_task_name=f"{celery_task_name}:{celery_run_id}",
        celery_registered_tasks=tuple(sorted(celery_app.registered)),
        redis_components=(
            type(redis_service.queue_backend).__name__,
            type(redis_service.repository).__name__,
            type(redis_service.events).__name__,
        ),
        redis_client_closed=redis_client.closed,
    )


async def _process_one_pull_job(
    runtime: ProductionRuntime,
    queue_backend: QueueBackend,
    repository: RunRepository,
) -> None:
    """执行一次显式 lease、CAS、worker、CAS 和 acknowledge 流程。"""
    lease = await queue_backend.lease("offline-worker", lease_seconds=30)
    if lease is None or lease.job.request is None:
        raise RuntimeError("pull queue did not provide a start job")
    current = await repository.get(lease.job.run_id)
    if current is None:
        raise RuntimeError("worker could not load the queued run record")
    running = replace(
        current,
        status=RunStatus.RUNNING,
        version=current.version + 1,
    )
    if not await repository.compare_and_set(current.run_id, current.version, running):
        await queue_backend.release(lease, delay_seconds=0)
        raise RuntimeError("worker failed to claim the run record")

    try:
        result = await runtime.worker_runtime.run(
            lease.job.request,
            run_id=lease.job.run_id,
        )
    except BaseException:
        await queue_backend.release(lease, delay_seconds=0)
        raise
    status = _run_status(result.status)
    completed = replace(
        running,
        status=status,
        result=result,
        error=result.error,
        version=running.version + 1,
    )
    if not await repository.compare_and_set(running.run_id, running.version, completed):
        await queue_backend.release(lease, delay_seconds=0)
        raise RuntimeError("worker failed to persist the completed run")
    await queue_backend.acknowledge(lease)


def _run_status(status: LoopStatus) -> RunStatus:
    """把 worker 的 Core 状态映射到队列查询状态。"""
    mapping = {
        LoopStatus.PAUSED: RunStatus.PAUSED,
        LoopStatus.BLOCKED: RunStatus.BLOCKED,
        LoopStatus.COMPLETED: RunStatus.COMPLETED,
        LoopStatus.FAILED: RunStatus.FAILED,
        LoopStatus.CANCELLED: RunStatus.CANCELLED,
        LoopStatus.TIMED_OUT: RunStatus.TIMED_OUT,
    }
    try:
        return mapping[status]
    except KeyError as exc:
        raise RuntimeError(f"worker returned unsettled status: {status.value}") from exc


def _route_paths(routes: Sequence[object]) -> tuple[str, ...]:
    """从 FastAPI Router 读取稳定的 MatterLoop 路由路径。"""
    paths: list[str] = []
    for route in routes:
        path = getattr(route, "path", None)
        if isinstance(path, str) and path.startswith("/loops"):
            paths.append(path)
    return tuple(sorted(paths))


def _build_worker_model() -> FakeModelClient:
    """创建依次响应 Planner、Worker 和 Verifier 的离线模型。"""
    return FakeModelClient(
        (
            ModelResponse(
                output_text=(
                    '{"steps":[{"description":"处理队列任务",'
                    '"executor":"default","acceptance_criteria":["独立验证通过"],'
                    '"requires_approval":false}]}'
                ),
                usage=TokenUsage(input_tokens=12, output_tokens=10, total_tokens=22),
            ),
            ModelResponse(
                output_text="队列 Worker 已完成离线任务。",
                usage=TokenUsage(input_tokens=10, output_tokens=8, total_tokens=18),
            ),
            ModelResponse(
                output_text=(
                    '{"passed":true,"score":100,"feedback":"通过",'
                    '"evidence":["离线 Worker 结果"],"failed_criteria":[]}'
                ),
                usage=TokenUsage(input_tokens=14, output_tokens=10, total_tokens=24),
            ),
        )
    )


def main() -> None:
    """运行示例并记录不包含任务正文、凭据和连接信息的摘要。"""
    logging.basicConfig(level=logging.INFO)
    result = asyncio.run(run_queued_example())
    logger.info(
        "队列示例完成",
        extra={
            "run_id": result.run_id,
            "status": result.status.value,
            "event_count": result.event_count,
            "celery_registered_tasks": result.celery_registered_tasks,
            "redis_components": result.redis_components,
        },
    )


if __name__ == "__main__":
    main()
