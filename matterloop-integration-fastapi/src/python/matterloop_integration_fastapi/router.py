"""MatterLoop FastAPI 路由工厂。"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from typing import Annotated, TypeVar, cast

from fastapi import APIRouter, Depends, HTTPException, Path, Query, status
from fastapi.encoders import jsonable_encoder
from matterloop_core import (
    InvalidStateTransitionError,
    LoopNotFoundError,
    LoopNotResumableError,
    MatterLoopError,
)
from matterloop_runtime import (
    DuplicateRunError,
    RunNotFoundError,
    RunNotResumableError,
    RuntimeClosedError,
)

from matterloop_integration_fastapi.protocols import (
    DirectRuntimeProtocol,
    QueueRuntimeProtocol,
    RuntimeProtocol,
)
from matterloop_integration_fastapi.schemas import (
    CancelResponse,
    CreateLoopRequest,
    EventListResponse,
    ResumeLoopRequest,
    ResumeResponse,
    RunResponse,
)

T = TypeVar("T")
RunId = Annotated[
    str,
    Path(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*$"),
]


def create_router(
    runtime: RuntimeProtocol,
    auth_dependency: Callable[..., object],
    prefix: str = "/loops",
) -> APIRouter:
    """创建只负责 HTTP 边界的 MatterLoop 路由。

    路由构建时会一次性识别队列运行时或直接运行时。队列运行时支持全部查询接口；直接
    运行时没有运行仓储，因此 `/list`、`/{run_id}` 和事件查询明确返回 HTTP 501。

    Args:
        runtime: 满足队列或直接异步运行结构协议的运行时。
        auth_dependency: 应用于全部路由的 FastAPI 鉴权依赖。
        prefix: 路由前缀，默认 `/loops`。

    Returns:
        可挂载到 FastAPI 应用的路由器。

    Raises:
        TypeError: 运行时或鉴权依赖不满足要求时抛出。
        ValueError: 路由前缀格式无效时抛出。
    """
    normalized_prefix = _normalize_prefix(prefix)
    if not callable(auth_dependency):
        raise TypeError("auth_dependency must be callable")

    is_queue_runtime = isinstance(runtime, QueueRuntimeProtocol)
    if is_queue_runtime:
        queue_runtime = cast(QueueRuntimeProtocol, runtime)
        direct_runtime: DirectRuntimeProtocol | None = None
    elif isinstance(runtime, DirectRuntimeProtocol):
        queue_runtime = None
        direct_runtime = cast(DirectRuntimeProtocol, runtime)
    else:
        raise TypeError("runtime must implement QueueRuntimeProtocol or DirectRuntimeProtocol")

    router = APIRouter(
        prefix=normalized_prefix,
        dependencies=[Depends(auth_dependency)],
        tags=["MatterLoop"],
    )

    @router.post("/create", response_model=RunResponse, status_code=status.HTTP_201_CREATED)
    async def create_loop(payload: CreateLoopRequest) -> RunResponse:
        """校验并提交新的 Loop 运行。"""
        request = payload.to_domain()
        if queue_runtime is not None:
            run_id = await _call(queue_runtime.submit(request, run_id=payload.run_id))
            record = await _call(queue_runtime.get(run_id))
            if record is None:
                raise HTTPException(
                    status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                    detail="运行记录创建后不可读取",
                )
            return RunResponse.from_record(record)
        assert direct_runtime is not None
        result = await _call(direct_runtime.run(request, run_id=payload.run_id))
        return RunResponse.from_result(result)

    @router.get("/list", response_model=list[RunResponse])
    async def list_loops(
        limit: Annotated[int, Query(ge=1, le=500)] = 100,
        offset: Annotated[int, Query(ge=0)] = 0,
    ) -> list[RunResponse]:
        """分页列出队列运行记录。"""
        queue = _require_queue_runtime(queue_runtime)
        records = await _call(queue.list(limit=limit, offset=offset))
        return [RunResponse.from_record(record) for record in records]

    @router.get("/{run_id}", response_model=RunResponse)
    async def get_loop(run_id: RunId) -> RunResponse:
        """读取单个队列运行记录。"""
        queue = _require_queue_runtime(queue_runtime)
        record = await _call(queue.get(run_id))
        if record is None:
            raise _not_found()
        return RunResponse.from_record(record)

    @router.post("/{run_id}/cancel", response_model=CancelResponse)
    async def cancel_loop(run_id: RunId) -> CancelResponse:
        """请求取消运行，不在 API 层等待协作式取消完成。"""
        active_runtime = queue_runtime if queue_runtime is not None else direct_runtime
        assert active_runtime is not None
        if queue_runtime is not None and await _call(queue_runtime.get(run_id)) is None:
            raise _not_found()
        accepted = await _call(active_runtime.cancel(run_id))
        return CancelResponse(run_id=run_id, accepted=accepted)

    @router.post("/{run_id}/resume", response_model=ResumeResponse)
    async def resume_loop(run_id: RunId, payload: ResumeLoopRequest) -> ResumeResponse:
        """按请求模式恢复运行并返回最新视图。"""
        if queue_runtime is not None:
            accepted = await _call(queue_runtime.resume(run_id, mode=payload.mode))
            record = await _call(queue_runtime.get(run_id))
            if record is None:
                raise _not_found()
            return ResumeResponse(accepted=accepted, run=RunResponse.from_record(record))
        assert direct_runtime is not None
        result = await _call(direct_runtime.resume(run_id, mode=payload.mode))
        return ResumeResponse(accepted=True, run=RunResponse.from_result(result))

    @router.get("/{run_id}/events/list", response_model=EventListResponse)
    async def list_loop_events(
        run_id: RunId,
        after: Annotated[str | None, Query(max_length=256)] = None,
        limit: Annotated[int, Query(ge=1, le=500)] = 100,
    ) -> EventListResponse:
        """分页读取队列运行的审计事件。"""
        queue = _require_queue_runtime(queue_runtime)
        if await _call(queue.get(run_id)) is None:
            raise _not_found()
        events = await _call(queue.list_events(run_id, after=after, limit=limit))
        return EventListResponse(items=tuple(_encode_event(event) for event in events))

    return router


async def _call(awaitable: Awaitable[T]) -> T:
    """调用运行时并把稳定异常映射为 HTTP 语义。"""
    try:
        return await awaitable
    except (LoopNotFoundError, RunNotFoundError) as exc:
        raise _not_found() from exc
    except (
        LoopNotResumableError,
        RunNotResumableError,
        InvalidStateTransitionError,
        DuplicateRunError,
    ) as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="运行状态与当前操作冲突",
        ) from exc
    except RuntimeClosedError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="运行时当前不可用",
        ) from exc
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="运行请求参数无效",
        ) from exc
    except MatterLoopError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="MatterLoop 请求无法处理",
        ) from exc


def _require_queue_runtime(
    runtime: QueueRuntimeProtocol | None,
) -> QueueRuntimeProtocol:
    """为缺少持久查询能力的直接运行时返回明确能力错误。"""
    if runtime is None:
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail="当前运行时未配置运行仓储",
        )
    return runtime


def _not_found() -> HTTPException:
    """创建不暴露内部运行标识的统一 404 响应。"""
    return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="运行不存在")


def _normalize_prefix(prefix: str) -> str:
    """规范化路由前缀并拒绝歧义路径。"""
    normalized = prefix.strip().rstrip("/")
    if not normalized.startswith("/") or normalized == "":
        raise ValueError("prefix must start with '/' and contain a path segment")
    if "{" in normalized or "}" in normalized:
        raise ValueError("prefix must not contain path parameters")
    return normalized


def _encode_event(event: Mapping[str, object]) -> dict[str, object]:
    """使用 FastAPI 编码器转换时间、枚举等常见事件值。"""
    encoded = jsonable_encoder(dict(event))
    if not isinstance(encoded, dict):
        raise TypeError("encoded event must be an object")
    return {str(key): value for key, value in encoded.items()}


__all__ = ["create_router"]
