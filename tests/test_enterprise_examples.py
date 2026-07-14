"""企业离线示例的端到端验收测试。"""

from matterloop_agents.collaboration import TeamStatus, TeamStopReason
from matterloop_core import LoopStatus
from matterloop_runtime import RunStatus

from examples.enterprise.embedded_agent import run_embedded_example
from examples.enterprise.queued_service import run_queued_example
from examples.enterprise.team_collaboration import run_team_example


async def test_embedded_example_completes_human_revision_and_budgeted_tool_flow() -> None:
    """单 Agent 示例应重规划且只执行一次安全工具调用。"""
    result = await run_embedded_example()

    assert result.status is LoopStatus.COMPLETED
    assert result.cycles == 2
    assert result.model_calls == 5
    assert result.tool_calls == 1
    assert result.human_feedback == ("删除写操作，只保留离线证据核验。",)
    assert "human.revised" in result.event_names
    assert result.event_names[-1] == "loop.completed"


async def test_team_example_fans_out_fans_in_and_stops_on_budget_exhaustion() -> None:
    """TeamLoop 示例应真实并行、汇总依赖并映射额度停止原因。"""
    result = await run_team_example()

    assert result.status is TeamStatus.COMPLETED
    assert result.cycles == 2
    assert result.maximum_parallel_tasks == 2
    assert result.agent_tasks == 6
    assert "dependencies=facts-2,risks-2" in result.output
    assert result.budget_stop_reason is TeamStopReason.BUDGET_EXHAUSTED
    assert result.human_feedback == ("最终摘要使用两条短句，并明确事实与风险。",)
    assert result.event_names[-1] == "team.completed"


async def test_queued_example_executes_pull_worker_and_wires_external_transports() -> None:
    """队列示例应完成 lease/CAS/ack，并展示互斥的 Celery 与 Redis 接线。"""
    result = await run_queued_example()

    assert result.status is RunStatus.COMPLETED
    assert result.event_count > 0
    assert result.api_routes == (
        "/loops/create",
        "/loops/list",
        "/loops/{run_id}",
        "/loops/{run_id}/cancel",
        "/loops/{run_id}/events/list",
        "/loops/{run_id}/resume",
    )
    assert result.celery_task_name == "matterloop.run:enterprise-celery-example"
    assert result.celery_registered_tasks == ("matterloop.resume", "matterloop.run")
    assert result.redis_components == (
        "RedisQueueBackend",
        "RedisRunRepository",
        "RedisEventPublisher",
    )
    assert result.redis_client_closed
