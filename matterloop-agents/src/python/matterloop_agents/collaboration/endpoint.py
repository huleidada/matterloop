"""把现有单 Agent Loop 适配为团队可调度端点。"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Protocol, runtime_checkable

from matterloop_core import LoopLimits, LoopRequest, LoopResult, LoopStatus
from matterloop_tools import ToolAccessScope

from matterloop_agents.collaboration._immutability import freeze_mapping
from matterloop_agents.collaboration.models import (
    AgentSpec,
    AgentTaskContext,
    TaskResult,
)


@runtime_checkable
class LoopRuntime(Protocol):
    """适配器所需的最小异步 Loop 运行接口。

    该结构协议避免 ``matterloop-agents`` 反向依赖 ``matterloop-runtime``；调用方可以
    注入 ``AsyncRuntime``、自定义远程运行时或测试替身。
    """

    async def run(self, request: LoopRequest, *, run_id: str | None = None) -> LoopResult:
        """执行一次 Loop 并返回结构化结果。"""
        ...


class LoopAgentEndpoint:
    """把一个已由用户构造的 Loop 运行时暴露为团队 Agent。

    Args:
        spec: Agent 的稳定标识、能力和并发上限。
        runtime: 用户显式构造并注入的异步 Loop 运行时。
        limits: 每个团队任务映射到 Loop 后使用的执行边界。
        metadata: 追加到每个 Loop 请求的只读业务元数据。
    """

    def __init__(
        self,
        spec: AgentSpec,
        runtime: LoopRuntime,
        *,
        limits: LoopLimits | None = None,
        metadata: Mapping[str, object] | None = None,
    ) -> None:
        self._spec = spec
        self._runtime = runtime
        self._limits = limits or LoopLimits()
        self._metadata = freeze_mapping(metadata or {})

    @property
    def spec(self) -> AgentSpec:
        """返回目录发现所需的不可变 Agent 规范。"""
        return self._spec

    async def execute(self, context: AgentTaskContext) -> TaskResult:
        """把团队任务映射成 Loop 请求并还原为团队任务结果。

        Args:
            context: 当前任务、依赖结果和尝试次数。

        Returns:
            包含 Loop 输出、制品和审计关联数据的任务结果。
        """
        criteria = context.task.acceptance_criteria or context.request.acceptance_criteria
        usage_scopes = self._usage_scopes(context)
        request = LoopRequest(
            goal=context.task.description,
            acceptance_criteria=criteria,
            limits=self._limits,
            metadata={
                **self._metadata,
                **dict(context.request.metadata),
                **dict(context.task.metadata),
                "team_run_id": context.team_run_id,
                "team_task_id": context.task.task_id,
                "team_agent_id": context.agent_id,
                "team_task_attempt": context.attempt,
                "previous_task_error": context.previous_error,
                "human_feedback": tuple(
                    {
                        "action": record.response.action.value,
                        "content": record.response.content,
                    }
                    for record in context.human_feedback
                ),
                "usage_scopes": usage_scopes,
                "dependency_outputs": tuple(result.output for result in context.dependency_results),
                # 团队任务元数据不可信；该保留字段必须最后写入，调用方不能提升子 Agent 权限。
                "tool_access_scope": ToolAccessScope.READ_ONLY.value,
            },
        )
        loop_run_id = (
            f"{context.team_run_id}--{context.task.task_id}--{context.agent_id}--{context.attempt}"
        )
        result = await self._runtime.run(request, run_id=loop_run_id)
        artifacts = tuple(
            artifact for record in result.records for artifact in record.execution.artifacts
        )
        success = result.status is LoopStatus.COMPLETED
        error = "" if success else (result.error or self._failure_message(result))
        return TaskResult(
            task_id=context.task.task_id,
            agent_id=context.agent_id,
            success=success,
            output=result.output,
            artifacts=artifacts,
            error=error,
            attempt=context.attempt,
            metadata={
                "loop_run_id": result.run_id,
                "loop_status": result.status.value,
                "loop_cycles": result.cycles,
                "loop_attempts": result.total_attempts,
                "loop_completed_steps": result.completed_steps,
                "loop_stop_reason": (
                    result.stop_reason.value if result.stop_reason is not None else None
                ),
                "loop_pending_interaction_id": (
                    result.pending_interaction.interaction_id
                    if result.pending_interaction is not None
                    else None
                ),
            },
        )

    @staticmethod
    def _usage_scopes(context: AgentTaskContext) -> tuple[str, ...]:
        """为子 Loop 构造 team/task/agent 多层额度汇总作用域。"""
        raw = context.request.metadata.get("usage_scopes", ())
        inherited = (
            tuple(item for item in raw if isinstance(item, str) and item.strip())
            if isinstance(raw, (tuple, list))
            else ()
        )
        derived = (
            f"team:{context.team_run_id}",
            f"task:{context.team_run_id}:{context.task.task_id}",
            f"agent:{context.agent_id}",
        )
        return tuple(dict.fromkeys((*inherited, *derived)))

    @staticmethod
    def _failure_message(result: LoopResult) -> str:
        reason = result.stop_reason.value if result.stop_reason is not None else "unknown"
        return f"loop did not complete successfully: status={result.status.value}, reason={reason}"


__all__ = ["LoopAgentEndpoint", "LoopRuntime"]
