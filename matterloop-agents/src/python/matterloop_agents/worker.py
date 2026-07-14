"""实现模型驱动且具有硬轮数边界的工具调用执行器。"""

from __future__ import annotations

import json
from collections.abc import Mapping

from matterloop_core import ExecutionResult, LoopContext, PlanStep
from matterloop_models import (
    MessageRole,
    ModelMessage,
    ModelRegistry,
    ModelRequest,
    TokenUsage,
    ToolDefinition,
    ToolOutput,
)
from matterloop_tools import ToolContext, ToolRegistry

from matterloop_agents.config import ToolCallingWorkerConfig
from matterloop_agents.errors import (
    AgentModelOutputError,
    ToolContinuationError,
    ToolLoopLimitError,
    UnauthorizedToolCallError,
)


class ToolCallingWorker:
    """让模型在显式工具白名单内完成一个计划步骤。

    Args:
        models: 支持热替换的模型注册表。
        tools: 统一执行权限检查和工具查找的注册表。
        config: 工具白名单和循环预算配置。
    """

    def __init__(
        self,
        models: ModelRegistry,
        tools: ToolRegistry,
        config: ToolCallingWorkerConfig,
    ) -> None:
        self._models = models
        self._tools = tools
        self._config = config

    async def execute(self, step: PlanStep, context: LoopContext) -> ExecutionResult:
        """执行步骤，并把每轮工具结果反馈给模型直到产生最终文本。

        Args:
            step: 当前待执行计划步骤。
            context: Loop 运行上下文。

        Returns:
            最终模型输出及不含敏感内容的调用统计。

        Raises:
            UnauthorizedToolCallError: 模型请求了白名单之外的工具。
            ToolContinuationError: 工具调用响应缺少响应标识。
            ToolLoopLimitError: 工具调用达到硬性轮数上限。
            AgentModelOutputError: 模型既没有工具调用也没有最终文本。
        """
        definitions = self._tool_definitions()
        request = ModelRequest(
            messages=(
                ModelMessage(
                    MessageRole.DEVELOPER,
                    "你是执行 Agent。只处理当前步骤；需要外部能力时使用已提供工具，"
                    "完成后返回清晰结果和证据。",
                ),
                ModelMessage(MessageRole.USER, self._step_payload(step, context)),
            ),
            tools=definitions,
            max_output_tokens=self._config.max_output_tokens,
            usage_scopes=self._usage_scopes(context),
            metadata={"run_id": context.run_id, "step_id": step.step_id, "agent": "worker"},
        )
        total_usage = TokenUsage()
        call_audit: list[Mapping[str, object]] = []

        # 一次完整工具事务固定同一客户端，热替换只影响后续新事务。
        async with self._models.acquire(self._config.model) as model:
            for tool_round in range(self._config.max_tool_rounds + 1):
                response = await model.generate(request)
                total_usage = self._merge_usage(total_usage, response.usage)
                if not response.tool_calls:
                    if not response.output_text.strip():
                        raise AgentModelOutputError(
                            "worker model returned neither text nor tool calls"
                        )
                    return ExecutionResult(
                        output=response.output_text,
                        metadata={
                            "model": self._config.model,
                            "tool_calls": tuple(call_audit),
                            "input_tokens": total_usage.input_tokens,
                            "output_tokens": total_usage.output_tokens,
                            "total_tokens": total_usage.total_tokens,
                            "cache_hit_tokens": total_usage.cache_hit_tokens,
                            "cache_miss_tokens": total_usage.cache_miss_tokens,
                            "reasoning_tokens": total_usage.reasoning_tokens,
                        },
                    )
                if tool_round >= self._config.max_tool_rounds:
                    raise ToolLoopLimitError(
                        f"worker exceeded {self._config.max_tool_rounds} tool rounds"
                    )
                if response.response_id is None and response.continuation is None:
                    raise ToolContinuationError(
                        "worker tool call response has no continuation state"
                    )

                outputs: list[ToolOutput] = []
                for call in response.tool_calls:
                    if call.name not in self._config.tool_names:
                        raise UnauthorizedToolCallError(call.name)
                    tool_result = await self._tools.invoke(
                        call.name,
                        call.arguments,
                        context=ToolContext(
                            run_id=context.run_id,
                            step_id=step.step_id,
                            metadata={"goal": context.request.goal},
                        ),
                    )
                    outputs.append(
                        ToolOutput(
                            call_id=call.call_id,
                            output=tool_result.content,
                            is_error=tool_result.is_error,
                        )
                    )
                    call_audit.append(
                        {
                            "call_id": call.call_id,
                            "tool": call.name,
                            "is_error": tool_result.is_error,
                        }
                    )
                request = ModelRequest(
                    messages=(),
                    tools=definitions,
                    tool_outputs=tuple(outputs),
                    previous_response_id=response.response_id,
                    continuation=response.continuation,
                    max_output_tokens=self._config.max_output_tokens,
                    usage_scopes=self._usage_scopes(context),
                    metadata={
                        "run_id": context.run_id,
                        "step_id": step.step_id,
                        "agent": "worker",
                    },
                )

        raise ToolLoopLimitError("worker tool loop ended unexpectedly")

    def _tool_definitions(self) -> tuple[ToolDefinition, ...]:
        definitions: list[ToolDefinition] = []
        for name in self._config.tool_names:
            spec = self._tools.get(name).spec
            definitions.append(
                ToolDefinition(
                    name=spec.name,
                    description=spec.description,
                    parameters=spec.input_schema,
                )
            )
        return tuple(definitions)

    @staticmethod
    def _step_payload(step: PlanStep, context: LoopContext) -> str:
        return json.dumps(
            {
                "goal": context.request.goal,
                "step_id": step.step_id,
                "step": step.description,
                "acceptance_criteria": list(step.acceptance_criteria),
                "latest_feedback": context.feedback,
            },
            ensure_ascii=False,
        )

    @staticmethod
    def _merge_usage(current: TokenUsage, addition: TokenUsage) -> TokenUsage:
        return TokenUsage(
            input_tokens=current.input_tokens + addition.input_tokens,
            output_tokens=current.output_tokens + addition.output_tokens,
            total_tokens=current.total_tokens + addition.total_tokens,
            cache_hit_tokens=current.cache_hit_tokens + addition.cache_hit_tokens,
            cache_miss_tokens=current.cache_miss_tokens + addition.cache_miss_tokens,
            reasoning_tokens=current.reasoning_tokens + addition.reasoning_tokens,
        )

    @staticmethod
    def _usage_scopes(context: LoopContext) -> tuple[str, ...]:
        """从请求元数据中读取由组合根显式声明的额度作用域。"""
        value = context.request.metadata.get("usage_scopes", ())
        if not isinstance(value, (tuple, list)):
            return ()
        return tuple(item for item in value if isinstance(item, str) and item.strip())
