"""预设装配结果与生产队列/worker 组合运行时。"""

from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType

from matterloop_core import (
    AgentLoop,
    CheckpointStore,
    HumanResponse,
    LoopRequest,
    LoopResult,
    ResumeMode,
)
from matterloop_models import ModelRegistry
from matterloop_runtime import (
    AsyncClosable,
    AsyncRuntime,
    LocalRuntime,
    QueueRuntime,
    RunRecord,
)
from matterloop_tools import ToolRegistry

from matterloop_presets.config import AgentPresetConfig


class PresetRuntime(AsyncRuntime):
    """保留可热替换组件入口的异步预设运行时。

    Args:
        loop: 已完成装配的核心控制器。
        models: 运行时模型注册表。
        tool_registries: 按执行器名称隔离的工具注册表。
        checkpoint_store: 当前 Loop 使用的检查点存储。
        config: 构建该运行时的不可变预设配置。
        resources: 退出异步上下文时需要逆序关闭的资源。
    """

    def __init__(
        self,
        loop: AgentLoop,
        models: ModelRegistry,
        tool_registries: Mapping[str, ToolRegistry],
        checkpoint_store: CheckpointStore,
        config: AgentPresetConfig,
        *,
        resources: tuple[AsyncClosable, ...] = (),
    ) -> None:
        super().__init__(loop, resources=resources)
        self.loop = loop
        self.models = models
        self.tool_registries = MappingProxyType(dict(tool_registries))
        self.checkpoint_store = checkpoint_store
        self.config = config


class ProductionRuntime:
    """组合生产队列客户端和实际执行 Loop 的 worker runtime。

    API 服务使用本对象的 `submit/get/list/result/wait/cancel/resume/list_events`；队列 worker
    租用命令后使用 `worker_runtime.run` 或 `worker_runtime.resume`，并自行完成仓储 CAS、租约
    acknowledge/release。预设不启动后台 worker，也不隐藏部署方的投递语义。
    """

    def __init__(self, queue_runtime: QueueRuntime, worker_runtime: PresetRuntime) -> None:
        self.queue_runtime = queue_runtime
        self.worker_runtime = worker_runtime

    async def submit(self, request: LoopRequest, *, run_id: str | None = None) -> str:
        """向生产队列提交新运行。"""
        return await self.queue_runtime.submit(request, run_id=run_id)

    async def get(self, run_id: str) -> RunRecord | None:
        """读取生产运行记录。"""
        return await self.queue_runtime.get(run_id)

    async def list(self, *, limit: int = 100, offset: int = 0) -> tuple[RunRecord, ...]:
        """分页列出生产运行记录。"""
        return await self.queue_runtime.list(limit=limit, offset=offset)

    async def result(self, run_id: str) -> LoopResult | None:
        """读取运行已经产生的最终结果。"""
        return await self.queue_runtime.result(run_id)

    async def wait(
        self,
        run_id: str,
        *,
        timeout_seconds: float | None = None,
        poll_interval_seconds: float = 0.1,
    ) -> RunRecord:
        """等待生产运行进入稳定状态。"""
        return await self.queue_runtime.wait(
            run_id,
            timeout_seconds=timeout_seconds,
            poll_interval_seconds=poll_interval_seconds,
        )

    async def cancel(self, run_id: str) -> bool:
        """请求取消生产队列中的运行。"""
        return await self.queue_runtime.cancel(run_id)

    async def resume(
        self,
        run_id: str,
        *,
        mode: ResumeMode = ResumeMode.CONTINUE,
    ) -> bool:
        """把暂停或阻塞的运行重新加入生产队列。"""
        return await self.queue_runtime.resume(run_id, mode=mode)

    async def list_events(
        self,
        run_id: str,
        *,
        after: str | None = None,
        limit: int = 100,
    ) -> tuple[Mapping[str, object], ...]:
        """读取生产运行的审计事件。"""
        return await self.queue_runtime.list_events(run_id, after=after, limit=limit)

    async def aclose(self) -> None:
        """关闭 worker runtime 持有的模型和工具资源。"""
        await self.worker_runtime.aclose()

    async def __aenter__(self) -> ProductionRuntime:
        """返回生产组合运行时。"""
        return self

    async def __aexit__(self, exc_type: object, exc: object, traceback: object) -> None:
        """退出上下文时释放 worker 资源。"""
        await self.aclose()


class ProductionLocalRuntime:
    """生产 worker 的同步门面，同时保留异步队列客户端。

    `run/resume/cancel` 只操作本地 worker runtime。API 或调度代码应通过公开的
    `queue_runtime` 属性执行异步队列操作。
    """

    def __init__(self, runtime: ProductionRuntime) -> None:
        self.queue_runtime = runtime.queue_runtime
        self.worker_runtime = LocalRuntime(runtime.worker_runtime)

    def create_run_id(self) -> str:
        """通过本地 worker 创建运行标识。"""
        return self.worker_runtime.create_run_id()

    def run(self, request: LoopRequest, *, run_id: str | None = None) -> LoopResult:
        """同步执行 worker 的新运行。"""
        return self.worker_runtime.run(request, run_id=run_id)

    def resume(
        self,
        run_id: str,
        *,
        mode: ResumeMode = ResumeMode.CONTINUE,
    ) -> LoopResult:
        """同步恢复 worker 检查点。"""
        return self.worker_runtime.resume(run_id, mode=mode)

    def submit_human_response(
        self,
        run_id: str,
        response: HumanResponse,
    ) -> LoopResult:
        """向本地 worker 检查点提交人工反馈，不自动恢复。"""
        return self.worker_runtime.submit_human_response(run_id, response)

    def cancel(self, run_id: str) -> bool:
        """请求取消本地 worker 当前运行。"""
        return self.worker_runtime.cancel(run_id)

    def close(self) -> None:
        """关闭本地 worker 线程及其异步资源。"""
        self.worker_runtime.close()

    def __enter__(self) -> ProductionLocalRuntime:
        """返回当前同步生产 worker 门面。"""
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        """退出上下文时关闭 worker。"""
        self.close()


__all__ = ["PresetRuntime", "ProductionLocalRuntime", "ProductionRuntime"]
