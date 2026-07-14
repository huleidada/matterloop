"""实现模型驱动、支持记忆检索的结构化规划器。"""

from __future__ import annotations

import json
from collections.abc import Mapping

from matterloop_core import LoopContext, Plan, PlanStep
from matterloop_memory import MemoryQuery, MemoryStore
from matterloop_models import (
    MessageRole,
    ModelMessage,
    ModelRegistry,
    ModelRequest,
)

from matterloop_agents._parsing import parse_json_object, require_boolean, require_string
from matterloop_agents.config import ModelPlannerConfig
from matterloop_agents.errors import AgentModelOutputError, PlanStepLimitError

_PLAN_SCHEMA: Mapping[str, object] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "steps": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "description": {"type": "string"},
                    "executor": {"type": "string"},
                    "acceptance_criteria": {
                        "type": "array",
                        "items": {"type": "string"},
                    },
                    "requires_approval": {"type": "boolean"},
                },
                "required": [
                    "description",
                    "executor",
                    "acceptance_criteria",
                    "requires_approval",
                ],
            },
        }
    },
    "required": ["steps"],
}


class ModelPlanner:
    """使用注册表中的模型生成有序、可验证的执行计划。

    Args:
        models: 支持热替换的模型注册表。
        config: 规划器不可变配置。
        memory: 可选长期记忆存储；查询失败会交给内核重试策略处理。
    """

    def __init__(
        self,
        models: ModelRegistry,
        config: ModelPlannerConfig,
        *,
        memory: MemoryStore | None = None,
    ) -> None:
        self._models = models
        self._config = config
        self._memory = memory

    async def plan(self, context: LoopContext) -> Plan:
        """根据目标、反馈、历史证据和相关记忆生成计划。

        Args:
            context: 当前 Loop 运行上下文。

        Returns:
            按顺序执行的结构化计划。

        Raises:
            AgentModelOutputError: 模型返回空计划或字段非法。
            PlanStepLimitError: 模型返回的步骤数超过配置限制。
        """
        memories = await self._load_memories(context)
        request = ModelRequest(
            messages=(
                ModelMessage(
                    MessageRole.DEVELOPER,
                    "你是工程规划器。把目标拆成最少且可独立验证的有序步骤；"
                    "不要执行任务，只返回符合 Schema 的计划。",
                ),
                ModelMessage(MessageRole.USER, self._context_payload(context, memories)),
            ),
            response_schema=_PLAN_SCHEMA,
            response_schema_name="matterloop_plan",
            max_output_tokens=self._config.max_output_tokens,
            usage_scopes=self._usage_scopes(context),
            metadata={"run_id": context.run_id, "agent": "planner"},
        )
        # 租约保证调用期间客户端不会被热替换提前关闭。
        async with self._models.acquire(self._config.model) as model:
            response = await model.generate(request)
        return self._parse_plan(response.output_text)

    @staticmethod
    def _usage_scopes(context: LoopContext) -> tuple[str, ...]:
        """读取由组合根显式注入的额度作用域。"""
        raw = context.request.metadata.get("usage_scopes", ())
        if not isinstance(raw, (tuple, list)):
            return ()
        return tuple(item for item in raw if isinstance(item, str) and item.strip())

    async def _load_memories(self, context: LoopContext) -> tuple[str, ...]:
        if self._memory is None:
            return ()
        matches = await self._memory.search(
            MemoryQuery(
                namespace=self._config.memory_namespace,
                text=context.request.goal,
                limit=self._config.memory_limit,
            )
        )
        return tuple(match.record.content for match in matches)

    @staticmethod
    def _context_payload(context: LoopContext, memories: tuple[str, ...]) -> str:
        records = [
            {
                "step": record.step.description,
                "passed": record.verification.passed,
                "feedback": record.verification.feedback,
            }
            for record in context.records
        ]
        return json.dumps(
            {
                "goal": context.request.goal,
                "acceptance_criteria": list(context.request.acceptance_criteria),
                "latest_feedback": context.feedback,
                "previous_attempts": records,
                "relevant_memories": list(memories),
            },
            ensure_ascii=False,
        )

    def _parse_plan(self, text: str) -> Plan:
        value = parse_json_object(text, purpose="planner")
        raw_steps = value.get("steps")
        if not isinstance(raw_steps, list) or not raw_steps:
            raise AgentModelOutputError("planner.steps must be a non-empty array")
        if len(raw_steps) > self._config.max_steps:
            raise PlanStepLimitError(
                f"planner returned {len(raw_steps)} steps; limit is {self._config.max_steps}"
            )
        steps: list[PlanStep] = []
        for index, item in enumerate(raw_steps):
            purpose = f"planner.steps[{index}]"
            if not isinstance(item, dict) or not all(isinstance(key, str) for key in item):
                raise AgentModelOutputError(f"{purpose} must be a JSON object")
            step = item
            criteria = step.get("acceptance_criteria")
            if not isinstance(criteria, list) or not all(
                isinstance(entry, str) and bool(entry.strip()) for entry in criteria
            ):
                raise AgentModelOutputError(
                    f"{purpose}.acceptance_criteria must contain non-empty strings"
                )
            executor = step.get("executor", self._config.default_executor)
            if not isinstance(executor, str) or not executor.strip():
                executor = self._config.default_executor
            steps.append(
                PlanStep(
                    description=require_string(step, "description", purpose=purpose),
                    executor=executor,
                    acceptance_criteria=tuple(criteria),
                    requires_approval=require_boolean(step, "requires_approval", purpose=purpose),
                )
            )
        return Plan(steps=tuple(steps))
