"""提供由显式模型注册表驱动的团队规划、验证和结果聚合组件。"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import cast

from matterloop_core import (
    HumanAction,
    HumanInteractionKind,
    HumanInteractionRequest,
)
from matterloop_models import MessageRole, ModelMessage, ModelRegistry, ModelRequest

from matterloop_agents._parsing import (
    parse_json_object,
    require_boolean,
    require_score,
    require_string,
    string_tuple,
)
from matterloop_agents.collaboration.errors import InvalidTaskGraphError
from matterloop_agents.collaboration.models import (
    AgentTaskContext,
    TaskResult,
    TaskSpec,
    TaskVerification,
    TeamPlanningContext,
    TeamRequest,
    TeamReview,
    TeamReviewAction,
    TeamReviewContext,
)
from matterloop_agents.collaboration.task_graph import TaskGraph
from matterloop_agents.errors import AgentModelOutputError

_TASK_FIELDS = frozenset(
    {
        "task_id",
        "description",
        "capability",
        "dependencies",
        "acceptance_criteria",
        "requires_approval",
        "priority",
    }
)
_VERIFICATION_FIELDS = frozenset({"passed", "score", "feedback", "evidence", "failed_criteria"})
_TEAM_REVIEW_FIELDS = frozenset(
    {"action", "score", "feedback", "evidence", "failed_criteria", "human_prompt"}
)


def _validate_model_config(model: str, max_output_tokens: int) -> None:
    """校验所有模型团队组件共享的配置边界。"""
    if not model.strip():
        raise ValueError("model registry name must not be empty")
    if max_output_tokens < 1:
        raise ValueError("max output tokens must be at least 1")


def _require_exact_keys(
    value: Mapping[str, object],
    expected: frozenset[str],
    *,
    purpose: str,
) -> None:
    """拒绝缺失字段和 Schema 之外的字段。"""
    actual = frozenset(value)
    if actual == expected:
        return
    missing = ", ".join(sorted(expected - actual)) or "none"
    unexpected = ", ".join(sorted(actual - expected)) or "none"
    raise AgentModelOutputError(
        f"{purpose} fields do not match schema; missing: {missing}; unexpected: {unexpected}"
    )


def _require_object(value: object, *, purpose: str) -> Mapping[str, object]:
    """读取只包含字符串键的 JSON 对象。"""
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise AgentModelOutputError(f"{purpose} must be a JSON object")
    return cast(dict[str, object], value)


def _require_integer(value: Mapping[str, object], key: str, *, purpose: str) -> int:
    """读取严格整数，禁止把布尔值当作优先级。"""
    item = value.get(key)
    if isinstance(item, bool) or not isinstance(item, int):
        raise AgentModelOutputError(f"{purpose}.{key} must be an integer")
    return item


def _planner_schema(
    max_tasks: int,
    capabilities: tuple[str, ...] = (),
) -> Mapping[str, object]:
    """构造带本次硬上限的团队任务 JSON Schema。"""
    task_schema: Mapping[str, object] = {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "task_id": {"type": "string", "minLength": 1},
            "description": {"type": "string", "minLength": 1},
            "capability": (
                {"type": "string", "enum": list(capabilities)}
                if capabilities
                else {"type": "string", "minLength": 1}
            ),
            "dependencies": {
                "type": "array",
                "items": {"type": "string", "minLength": 1},
                "uniqueItems": True,
            },
            "acceptance_criteria": {
                "type": "array",
                "items": {"type": "string", "minLength": 1},
            },
            "requires_approval": {"type": "boolean"},
            "priority": {"type": "integer"},
        },
        "required": sorted(_TASK_FIELDS),
    }
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "tasks": {
                "type": "array",
                "minItems": 1,
                "maxItems": max_tasks,
                "items": task_schema,
            }
        },
        "required": ["tasks"],
    }


_VERIFICATION_SCHEMA: Mapping[str, object] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "passed": {"type": "boolean"},
        "score": {"type": "number", "minimum": 0, "maximum": 100},
        "feedback": {"type": "string"},
        "evidence": {
            "type": "array",
            "items": {"type": "string", "minLength": 1},
        },
        "failed_criteria": {
            "type": "array",
            "items": {"type": "string", "minLength": 1},
        },
    },
    "required": sorted(_VERIFICATION_FIELDS),
}

_AGGREGATION_SCHEMA: Mapping[str, object] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {"summary": {"type": "string", "minLength": 1}},
    "required": ["summary"],
}

_TEAM_REVIEW_SCHEMA: Mapping[str, object] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "action": {
            "type": "string",
            "enum": [item.value for item in TeamReviewAction],
        },
        "score": {"type": "number", "minimum": 0, "maximum": 100},
        "feedback": {"type": "string"},
        "evidence": {"type": "array", "items": {"type": "string", "minLength": 1}},
        "failed_criteria": {
            "type": "array",
            "items": {"type": "string", "minLength": 1},
        },
        "human_prompt": {"type": "string"},
    },
    "required": sorted(_TEAM_REVIEW_FIELDS),
}


@dataclass(frozen=True, slots=True)
class ModelTeamPlannerConfig:
    """配置模型团队规划器。

    Args:
        model: 每次规划时从注册表重新解析的模型名称。
        max_tasks: 单次模型规划允许返回的最大任务数。
        max_output_tokens: 规划响应允许使用的最大输出 Token 数。
    """

    model: str
    max_tasks: int = 20
    max_output_tokens: int = 4096

    def __post_init__(self) -> None:
        """拒绝空模型名和没有硬边界的规划配置。"""
        _validate_model_config(self.model, self.max_output_tokens)
        if self.max_tasks < 1:
            raise ValueError("max tasks must be at least 1")


@dataclass(frozen=True, slots=True)
class ModelTaskVerifierConfig:
    """配置模型任务验证器。

    Args:
        model: 每次验证时从注册表重新解析的模型名称。
        pass_score: 模型声明通过后仍必须达到的最低分数。
        max_output_tokens: 验证响应允许使用的最大输出 Token 数。
    """

    model: str
    pass_score: float = 80.0
    max_output_tokens: int = 2048

    def __post_init__(self) -> None:
        """校验评分阈值和模型调用边界。"""
        _validate_model_config(self.model, self.max_output_tokens)
        if not 0 <= self.pass_score <= 100:
            raise ValueError("pass score must be between 0 and 100")


@dataclass(frozen=True, slots=True)
class ModelResultAggregatorConfig:
    """配置模型团队结果聚合器。

    Args:
        model: 每次聚合时从注册表重新解析的模型名称。
        max_output_tokens: 聚合响应允许使用的最大输出 Token 数。
    """

    model: str
    max_output_tokens: int = 4096

    def __post_init__(self) -> None:
        """校验模型名称和输出预算。"""
        _validate_model_config(self.model, self.max_output_tokens)


@dataclass(frozen=True, slots=True)
class ModelTeamReviewerConfig:
    """配置模型团队审查器。

    Args:
        model: 每次审查时从注册表解析的模型名称。
        pass_score: 接受总体目标时需要达到的最低分数。
        max_output_tokens: 审查结构化响应的最大输出 Token 数。
    """

    model: str
    pass_score: float = 80.0
    max_output_tokens: int = 2048

    def __post_init__(self) -> None:
        """校验模型名称、评分阈值和输出边界。"""
        _validate_model_config(self.model, self.max_output_tokens)
        if not 0 <= self.pass_score <= 100:
            raise ValueError("pass score must be between 0 and 100")


class ModelTeamPlanner:
    """使用显式注入的模型注册表生成可调度任务 DAG。

    Args:
        models: 由应用层构造并注册客户端的模型注册表。
        config: 模型名称、任务数量和输出预算配置。

    Note:
        组件不读取环境变量，也不构造模型客户端。每次调用前都会重新从注册表
        解析客户端，因此热替换只影响后续调用。
    """

    def __init__(self, models: ModelRegistry, config: ModelTeamPlannerConfig) -> None:
        self._models = models
        self._config = config

    async def plan(
        self,
        context: TeamPlanningContext | TeamRequest,
    ) -> tuple[TaskSpec, ...]:
        """把团队目标拆成经过 DAG 校验的任务定义。

        Args:
            context: 团队循环上下文。仍接受 ``TeamRequest`` 作为 0.1
                过渡调用，但此时不具备能力快照约束。

        Returns:
            模型顺序下的不可变任务定义元组。

        Raises:
            AgentModelOutputError: 模型输出不符合 Schema、超过硬限制或不是有效 DAG。
        """
        if isinstance(context, TeamRequest):
            request = context
            capabilities: tuple[str, ...] = ()
            planning_payload: dict[str, object] = {}
            usage_scopes = self._usage_scopes(request)
        else:
            request = context.request
            capabilities = tuple(sorted(context.available_capabilities))
            if not capabilities:
                raise AgentModelOutputError("team planner has no registered capabilities")
            planning_payload = {
                "run_id": context.run_id,
                "cycle": context.cycle,
                "plan_revision": context.plan_revision,
                "available_agents": [
                    {
                        "agent_id": agent.agent_id,
                        "role": agent.role,
                        "capabilities": sorted(agent.capabilities),
                        "description": agent.description,
                    }
                    for agent in context.available_agents
                ],
                "prior_reviews": [
                    {
                        "action": review.action.value,
                        "feedback": review.feedback,
                        "failed_criteria": list(review.failed_criteria),
                    }
                    for review in context.prior_reviews
                ],
                "human_feedback": [
                    {
                        "action": record.response.action.value,
                        "content": record.response.content,
                    }
                    for record in context.human_feedback
                ],
            }
            usage_scopes = self._usage_scopes(request, context.run_id)
        max_tasks = min(self._config.max_tasks, request.limits.max_tasks)
        model_request = ModelRequest(
            messages=(
                ModelMessage(
                    MessageRole.DEVELOPER,
                    "你是多智能体团队规划器。把目标拆成最少的可独立验收任务，"
                    "为每个任务选择稳定能力标签，使用依赖标识组成无环图。"
                    "不要执行任务，只返回符合 Schema 的 JSON。",
                ),
                ModelMessage(
                    MessageRole.USER,
                    json.dumps(
                        {
                            "goal": request.goal,
                            "acceptance_criteria": list(request.acceptance_criteria),
                            "max_tasks": max_tasks,
                            **planning_payload,
                        },
                        ensure_ascii=False,
                    ),
                ),
            ),
            response_schema=_planner_schema(max_tasks, capabilities),
            response_schema_name="matterloop_team_plan",
            max_output_tokens=self._config.max_output_tokens,
            usage_scopes=usage_scopes,
            metadata={"agent": "team_planner"},
        )
        async with self._models.acquire(self._config.model) as model:
            response = await model.generate(model_request)
        return self._parse_tasks(
            response.output_text,
            max_tasks=max_tasks,
            allowed_capabilities=frozenset(capabilities),
        )

    @staticmethod
    def _parse_tasks(
        text: str,
        *,
        max_tasks: int,
        allowed_capabilities: frozenset[str] = frozenset(),
    ) -> tuple[TaskSpec, ...]:
        value = parse_json_object(text, purpose="team_planner")
        _require_exact_keys(value, frozenset({"tasks"}), purpose="team_planner")
        raw_tasks = value.get("tasks")
        if not isinstance(raw_tasks, list) or not raw_tasks:
            raise AgentModelOutputError("team_planner.tasks must be a non-empty array")
        if len(raw_tasks) > max_tasks:
            raise AgentModelOutputError(
                f"team_planner returned {len(raw_tasks)} tasks; limit is {max_tasks}"
            )

        tasks: list[TaskSpec] = []
        for index, raw_task in enumerate(raw_tasks):
            purpose = f"team_planner.tasks[{index}]"
            item = _require_object(raw_task, purpose=purpose)
            _require_exact_keys(item, _TASK_FIELDS, purpose=purpose)
            try:
                task = TaskSpec(
                    task_id=require_string(item, "task_id", purpose=purpose),
                    description=require_string(item, "description", purpose=purpose),
                    capability=require_string(item, "capability", purpose=purpose),
                    dependencies=string_tuple(item, "dependencies", purpose=purpose),
                    acceptance_criteria=string_tuple(
                        item,
                        "acceptance_criteria",
                        purpose=purpose,
                    ),
                    requires_approval=require_boolean(
                        item,
                        "requires_approval",
                        purpose=purpose,
                    ),
                    priority=_require_integer(item, "priority", purpose=purpose),
                )
                if allowed_capabilities and task.capability not in allowed_capabilities:
                    raise AgentModelOutputError(
                        f"{purpose}.capability is not registered: {task.capability}"
                    )
                tasks.append(task)
            except ValueError as exc:
                raise AgentModelOutputError(f"{purpose} is invalid: {exc}") from exc

        result = tuple(tasks)
        try:
            TaskGraph(result)
        except InvalidTaskGraphError as exc:
            raise AgentModelOutputError(f"team_planner returned an invalid DAG: {exc}") from exc
        return result

    @staticmethod
    def _usage_scopes(request: TeamRequest, run_id: str | None = None) -> tuple[str, ...]:
        """读取组合根注入的额度作用域，并追加团队运行域。"""
        raw = request.metadata.get("usage_scopes", ())
        scopes = (
            tuple(item for item in raw if isinstance(item, str) and item.strip())
            if isinstance(raw, (tuple, list))
            else ()
        )
        if run_id is None:
            return scopes
        team_scope = f"team:{run_id}"
        return scopes if team_scope in scopes else (*scopes, team_scope)


class ModelTaskVerifier:
    """使用独立模型和保守规则验证一个团队任务结果。

    Args:
        models: 由应用层构造并注册客户端的模型注册表。
        config: 模型名称、通过阈值和输出预算配置。
    """

    def __init__(self, models: ModelRegistry, config: ModelTaskVerifierConfig) -> None:
        self._models = models
        self._config = config

    async def verify(
        self,
        context: AgentTaskContext,
        result: TaskResult,
    ) -> TaskVerification:
        """依据任务条件、依赖结果和制品证据返回保守验证结论。

        Args:
            context: 当前团队、任务、分配 Agent 和依赖结果上下文。
            result: 等待独立验证的任务结果。

        Returns:
            只有执行成功、模型声明通过、达到阈值且无失败条件时才通过的结论。

        Raises:
            AgentModelOutputError: 模型验证输出缺失字段或字段类型非法。
        """
        model_request = ModelRequest(
            messages=(
                ModelMessage(
                    MessageRole.DEVELOPER,
                    "你是独立任务验证器。只依据给出的验收条件、输出和证据判断；"
                    "证据不足时必须判定不通过，只返回符合 Schema 的 JSON。",
                ),
                ModelMessage(
                    MessageRole.USER,
                    self._verification_payload(context, result),
                ),
            ),
            response_schema=_VERIFICATION_SCHEMA,
            response_schema_name="matterloop_team_task_verification",
            max_output_tokens=self._config.max_output_tokens,
            usage_scopes=ModelTeamPlanner._usage_scopes(
                context.request,
                context.team_run_id,
            ),
            metadata={
                "agent": "team_task_verifier",
                "team_run_id": context.team_run_id,
                "task_id": context.task.task_id,
            },
        )
        async with self._models.acquire(self._config.model) as model:
            response = await model.generate(model_request)
        value = parse_json_object(response.output_text, purpose="team_task_verifier")
        _require_exact_keys(value, _VERIFICATION_FIELDS, purpose="team_task_verifier")
        score = require_score(value, "score", purpose="team_task_verifier")
        failed_criteria = string_tuple(
            value,
            "failed_criteria",
            purpose="team_task_verifier",
        )
        model_passed = require_boolean(value, "passed", purpose="team_task_verifier")
        feedback = value.get("feedback")
        if not isinstance(feedback, str):
            raise AgentModelOutputError("team_task_verifier.feedback must be a string")
        return TaskVerification(
            passed=(
                result.success
                and model_passed
                and score >= self._config.pass_score
                and not failed_criteria
            ),
            feedback=feedback,
            score=score,
            evidence=string_tuple(value, "evidence", purpose="team_task_verifier"),
            failed_criteria=failed_criteria,
        )

    @staticmethod
    def _verification_payload(context: AgentTaskContext, result: TaskResult) -> str:
        criteria = context.task.acceptance_criteria or context.request.acceptance_criteria
        return json.dumps(
            {
                "team_goal": context.request.goal,
                "task": context.task.description,
                "acceptance_criteria": list(criteria),
                "execution": {
                    "success": result.success,
                    "output": result.output,
                    "error": result.error,
                    "attempt": result.attempt,
                },
                "artifacts": [
                    {
                        "name": artifact.name,
                        "uri": artifact.uri,
                        "media_type": artifact.media_type,
                    }
                    for artifact in result.artifacts
                ],
                "dependency_results": [
                    {
                        "task_id": dependency.task_id,
                        "success": dependency.success,
                        "output": dependency.output,
                    }
                    for dependency in context.dependency_results
                ],
            },
            ensure_ascii=False,
        )


class ModelResultAggregator:
    """使用模型把经过验证的任务结果整理成团队最终输出。

    Args:
        models: 由应用层构造并注册客户端的模型注册表。
        config: 模型名称和输出预算配置。

    Note:
        没有任务结果时不会调用模型，而是返回明确的无结果说明，避免模型凭空生成结论。
    """

    EMPTY_RESULT = "未产生可汇总的任务结果。"

    def __init__(self, models: ModelRegistry, config: ModelResultAggregatorConfig) -> None:
        self._models = models
        self._config = config

    async def aggregate(
        self,
        request: TeamRequest,
        results: tuple[TaskResult, ...],
    ) -> str:
        """汇总团队目标、验收条件和各任务输出。

        Args:
            request: 原始团队目标与验收条件。
            results: 已完成并通过团队编排器验证的任务结果。

        Returns:
            模型生成的非空总结；没有结果时返回稳定的无结果说明。

        Raises:
            AgentModelOutputError: 模型没有返回严格的非空总结对象。
        """
        if not results:
            return self.EMPTY_RESULT

        model_request = ModelRequest(
            messages=(
                ModelMessage(
                    MessageRole.DEVELOPER,
                    "你是团队结果聚合器。基于已完成任务的输出和制品引用形成准确总结，"
                    "不得补充任务结果中不存在的事实，只返回符合 Schema 的 JSON。",
                ),
                ModelMessage(
                    MessageRole.USER,
                    self._aggregation_payload(request, results),
                ),
            ),
            response_schema=_AGGREGATION_SCHEMA,
            response_schema_name="matterloop_team_result",
            max_output_tokens=self._config.max_output_tokens,
            usage_scopes=ModelTeamPlanner._usage_scopes(request),
            metadata={"agent": "team_result_aggregator"},
        )
        async with self._models.acquire(self._config.model) as model:
            response = await model.generate(model_request)
        value = parse_json_object(response.output_text, purpose="team_result_aggregator")
        _require_exact_keys(
            value,
            frozenset({"summary"}),
            purpose="team_result_aggregator",
        )
        return require_string(value, "summary", purpose="team_result_aggregator")

    @staticmethod
    def _aggregation_payload(
        request: TeamRequest,
        results: tuple[TaskResult, ...],
    ) -> str:
        return json.dumps(
            {
                "goal": request.goal,
                "acceptance_criteria": list(request.acceptance_criteria),
                "task_results": [
                    {
                        "task_id": result.task_id,
                        "agent_id": result.agent_id,
                        "success": result.success,
                        "output": result.output,
                        "error": result.error,
                        "attempt": result.attempt,
                        "artifacts": [
                            {
                                "name": artifact.name,
                                "uri": artifact.uri,
                                "media_type": artifact.media_type,
                            }
                            for artifact in result.artifacts
                        ],
                    }
                    for result in results
                ],
            },
            ensure_ascii=False,
        )


class ModelTeamReviewer:
    """使用独立模型对团队草稿执行总体目标验收。

    Args:
        models: 由应用组合根显式注入的模型注册表。
        config: 审查模型、通过阈值和输出边界。
    """

    def __init__(self, models: ModelRegistry, config: ModelTeamReviewerConfig) -> None:
        self._models = models
        self._config = config

    async def review(self, context: TeamReviewContext) -> TeamReview:
        """返回团队级接受、重规划、人工介入或停止决策。

        Args:
            context: 当前循环中通过任务级验收的结果和草稿。

        Returns:
            经本地保守规则复核的结构化审查结论。

        Raises:
            AgentModelOutputError: 模型审查结构或人工请求不完整。
        """
        model_request = ModelRequest(
            messages=(
                ModelMessage(
                    MessageRole.DEVELOPER,
                    "你是独立团队审查员。按总体目标和验收条件检查已验证"
                    "的任务草稿；证据不足时不得接受，只返回符合 Schema 的 JSON。",
                ),
                ModelMessage(MessageRole.USER, self._payload(context)),
            ),
            response_schema=_TEAM_REVIEW_SCHEMA,
            response_schema_name="matterloop_team_review",
            max_output_tokens=self._config.max_output_tokens,
            usage_scopes=ModelTeamPlanner._usage_scopes(
                context.request,
                context.run_id,
            ),
            metadata={
                "agent": "team_reviewer",
                "team_run_id": context.run_id,
                "cycle": context.cycle,
            },
        )
        async with self._models.acquire(self._config.model) as model:
            response = await model.generate(model_request)
        value = parse_json_object(response.output_text, purpose="team_reviewer")
        _require_exact_keys(value, _TEAM_REVIEW_FIELDS, purpose="team_reviewer")
        score = require_score(value, "score", purpose="team_reviewer")
        feedback = value.get("feedback")
        human_prompt = value.get("human_prompt")
        if not isinstance(feedback, str):
            raise AgentModelOutputError("team_reviewer.feedback must be a string")
        if not isinstance(human_prompt, str):
            raise AgentModelOutputError("team_reviewer.human_prompt must be a string")
        try:
            action = TeamReviewAction(require_string(value, "action", purpose="team_reviewer"))
        except ValueError as exc:
            raise AgentModelOutputError("team_reviewer.action is invalid") from exc
        evidence = string_tuple(value, "evidence", purpose="team_reviewer")
        failed = string_tuple(value, "failed_criteria", purpose="team_reviewer")
        if action is TeamReviewAction.ACCEPT and (score < self._config.pass_score or failed):
            action = TeamReviewAction.REPLAN
        interaction: HumanInteractionRequest | None = None
        if action is TeamReviewAction.REQUEST_HUMAN:
            if not human_prompt.strip():
                raise AgentModelOutputError(
                    "team_reviewer.human_prompt is required for request_human"
                )
            interaction = HumanInteractionRequest(
                kind=HumanInteractionKind.COMPLETION_REVIEW,
                prompt=human_prompt,
                allowed_actions=(
                    HumanAction.APPROVE,
                    HumanAction.REJECT,
                    HumanAction.REVISE,
                    HumanAction.PROVIDE_INPUT,
                ),
                metadata={
                    "team_run_id": context.run_id,
                    "cycle": context.cycle,
                    "source": "team_reviewer",
                },
            )
        return TeamReview(
            action=action,
            feedback=feedback,
            score=score,
            evidence=evidence,
            failed_criteria=failed,
            interaction=interaction,
        )

    @staticmethod
    def _payload(context: TeamReviewContext) -> str:
        """构造不包含提示词或隐式推理的团队验收输入。"""
        return json.dumps(
            {
                "goal": context.request.goal,
                "acceptance_criteria": list(context.request.acceptance_criteria),
                "cycle": context.cycle,
                "draft_output": context.draft_output,
                "task_results": [
                    {
                        "task_id": result.task_id,
                        "agent_id": result.agent_id,
                        "output": result.output,
                        "artifacts": [artifact.uri for artifact in result.artifacts],
                    }
                    for result in context.task_results
                ],
                "prior_reviews": [
                    {
                        "action": review.action.value,
                        "feedback": review.feedback,
                        "failed_criteria": list(review.failed_criteria),
                    }
                    for review in context.prior_reviews
                ],
                "human_feedback": [
                    {
                        "action": record.response.action.value,
                        "content": record.response.content,
                    }
                    for record in context.human_feedback
                ],
            },
            ensure_ascii=False,
        )


__all__ = [
    "ModelResultAggregator",
    "ModelResultAggregatorConfig",
    "ModelTaskVerifier",
    "ModelTaskVerifierConfig",
    "ModelTeamPlanner",
    "ModelTeamPlannerConfig",
    "ModelTeamReviewer",
    "ModelTeamReviewerConfig",
]
