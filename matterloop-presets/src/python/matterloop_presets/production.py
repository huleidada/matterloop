"""显式基础设施依赖的生产队列与 worker 预设。"""

from matterloop_core import ApprovalGate, CheckpointStore, EventPublisher
from matterloop_models import ModelClient
from matterloop_observability import CompositeEventPublisher, PublisherFailureMode
from matterloop_policies import AllowAllApproval
from matterloop_runtime import QueueBackend, QueueRuntime, RunEventReader, RunRepository
from matterloop_tools import ToolRegistry

from matterloop_presets._assembly import _assemble_runtime
from matterloop_presets.config import ProductionPresetConfig
from matterloop_presets.errors import PresetConfigurationError
from matterloop_presets.runtime import ProductionLocalRuntime, ProductionRuntime


def build_production_runtime(
    model: ModelClient,
    config: ProductionPresetConfig | None = None,
    *,
    queue_backend: QueueBackend | None = None,
    run_repository: RunRepository | None = None,
    checkpoint_store: CheckpointStore | None = None,
    audit_publisher: EventPublisher | None = None,
    event_reader: RunEventReader | None = None,
    approval_gate: ApprovalGate | None = None,
) -> ProductionRuntime:
    """构建显式基础设施依赖的生产队列与 worker 组合运行时。

    `queue_backend`、`run_repository`、`checkpoint_store` 和 `audit_publisher` 不提供内存回退；
    任一缺失都会在创建模型或后台任务前快速失败。

    Args:
        model: 生产 worker 使用的模型客户端。
        config: 可选不可变配置。
        queue_backend: 显式队列后端。
        run_repository: 显式运行状态仓储。
        checkpoint_store: 显式 Loop 检查点存储。
        audit_publisher: 显式审计事件发布器，发布失败会抛出。
        event_reader: 可选审计事件读取器。
        approval_gate: 可选生产审批实现。

    Returns:
        分离队列客户端与实际 Loop worker 的生产运行时。

    Raises:
        PresetConfigurationError: 缺少任何必需基础设施依赖。
    """
    missing = tuple(
        name
        for name, value in (
            ("queue_backend", queue_backend),
            ("run_repository", run_repository),
            ("checkpoint_store", checkpoint_store),
            ("audit_publisher", audit_publisher),
        )
        if value is None
    )
    if missing:
        raise PresetConfigurationError(
            f"production preset requires explicit dependencies: {', '.join(missing)}"
        )
    assert queue_backend is not None
    assert run_repository is not None
    assert checkpoint_store is not None
    assert audit_publisher is not None

    actual_config = config or ProductionPresetConfig()
    tools = ToolRegistry()
    worker_runtime = _assemble_runtime(
        model=model,
        config=actual_config,
        checkpoint_store=checkpoint_store,
        events=CompositeEventPublisher(
            (audit_publisher,),
            failure_mode=PublisherFailureMode.RAISE,
        ),
        approval_gate=approval_gate or AllowAllApproval(),
        tool_registries={"default": tools},
        executor_tools={"default": ()},
    )
    actual_event_reader = event_reader
    if actual_event_reader is None and isinstance(audit_publisher, RunEventReader):
        actual_event_reader = audit_publisher
    queue_runtime = QueueRuntime(
        queue_backend,
        run_repository,
        event_reader=actual_event_reader,
    )
    return ProductionRuntime(queue_runtime, worker_runtime)


def build_production_local_runtime(
    model: ModelClient,
    config: ProductionPresetConfig | None = None,
    *,
    queue_backend: QueueBackend | None = None,
    run_repository: RunRepository | None = None,
    checkpoint_store: CheckpointStore | None = None,
    audit_publisher: EventPublisher | None = None,
    event_reader: RunEventReader | None = None,
    approval_gate: ApprovalGate | None = None,
) -> ProductionLocalRuntime:
    """构建同步生产 worker，并通过属性保留异步队列客户端。

    Args:
        model: 生产 worker 使用的模型客户端。
        config: 可选不可变配置。
        queue_backend: 显式队列后端。
        run_repository: 显式运行状态仓储。
        checkpoint_store: 显式 Loop 检查点存储。
        audit_publisher: 显式审计事件发布器。
        event_reader: 可选审计事件读取器。
        approval_gate: 可选生产审批实现。

    Returns:
        同步 worker 门面与异步队列客户端的组合对象。
    """
    runtime = build_production_runtime(
        model,
        config,
        queue_backend=queue_backend,
        run_repository=run_repository,
        checkpoint_store=checkpoint_store,
        audit_publisher=audit_publisher,
        event_reader=event_reader,
        approval_gate=approval_gate,
    )
    return ProductionLocalRuntime(runtime)


__all__ = ["build_production_local_runtime", "build_production_runtime"]
