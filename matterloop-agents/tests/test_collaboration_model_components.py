"""模型驱动团队规划、验证和聚合组件测试。"""

from __future__ import annotations

import json
from dataclasses import FrozenInstanceError

import pytest
from matterloop_agents.collaboration.model_components import (
    ModelResultAggregator,
    ModelResultAggregatorConfig,
    ModelTaskVerifier,
    ModelTaskVerifierConfig,
    ModelTeamPlanner,
    ModelTeamPlannerConfig,
    ModelTeamReviewer,
    ModelTeamReviewerConfig,
)
from matterloop_agents.collaboration.models import (
    AgentSpec,
    AgentTaskContext,
    TaskResult,
    TaskSpec,
    TeamLimits,
    TeamPlanningContext,
    TeamRequest,
    TeamReviewAction,
    TeamReviewContext,
)
from matterloop_agents.errors import AgentModelOutputError
from matterloop_models import FakeModelClient, ModelRegistry, ModelResponse


def _task_data(
    task_id: str,
    description: str,
    *,
    capability: str = "python",
    dependencies: tuple[str, ...] = (),
    priority: int = 0,
) -> dict[str, object]:
    """构造包含全部 Schema 必填字段的模型任务数据。"""
    return {
        "task_id": task_id,
        "description": description,
        "capability": capability,
        "dependencies": list(dependencies),
        "acceptance_criteria": [f"{description}完成"],
        "requires_approval": False,
        "priority": priority,
    }


def _plan_response(*tasks: dict[str, object]) -> ModelResponse:
    """把任务数据编码成假模型响应。"""
    return ModelResponse(output_text=json.dumps({"tasks": list(tasks)}, ensure_ascii=False))


def _context() -> AgentTaskContext:
    """构造验证器使用的稳定任务上下文。"""
    request = TeamRequest("交付协作功能", acceptance_criteria=("全部测试通过",))
    return AgentTaskContext(
        team_run_id="team-run",
        request=request,
        task=TaskSpec(
            "implementation",
            "实现协作功能",
            "python",
            acceptance_criteria=("实现正确",),
        ),
        agent_id="python-agent",
        attempt=1,
        dependency_results=(TaskResult("design", "architect", True, output="设计完成"),),
    )


def _verification_response(
    *,
    passed: bool,
    score: float,
    failed_criteria: tuple[str, ...] = (),
    feedback: str = "验证完成",
) -> ModelResponse:
    """构造严格验证 Schema 对应的假模型响应。"""
    return ModelResponse(
        output_text=json.dumps(
            {
                "passed": passed,
                "score": score,
                "feedback": feedback,
                "evidence": ["测试报告"],
                "failed_criteria": list(failed_criteria),
            },
            ensure_ascii=False,
        )
    )


@pytest.mark.parametrize(
    "factory, message",
    [
        (lambda: ModelTeamPlannerConfig(""), "model registry name"),
        (lambda: ModelTeamPlannerConfig("planner", max_tasks=0), "max tasks"),
        (
            lambda: ModelTaskVerifierConfig("verifier", pass_score=101),
            "pass score",
        ),
        (
            lambda: ModelResultAggregatorConfig("aggregator", max_output_tokens=0),
            "max output tokens",
        ),
    ],
)
def test_model_team_component_configs_validate_boundaries(
    factory: object,
    message: str,
) -> None:
    """冻结配置必须拒绝空模型名和失控预算。"""
    with pytest.raises(ValueError, match=message):
        factory()  # type: ignore[operator]

    config = ModelTeamPlannerConfig("planner")
    with pytest.raises(FrozenInstanceError):
        config.max_tasks = 99  # type: ignore[misc]


async def test_model_team_planner_builds_strict_validated_dag_and_schema() -> None:
    """规划器必须声明严格 Schema，并生成经过依赖校验的任务 DAG。"""
    model = FakeModelClient(
        [
            _plan_response(
                _task_data("design", "完成设计", capability="architecture", priority=10),
                _task_data(
                    "implementation",
                    "完成实现",
                    dependencies=("design",),
                    priority=5,
                ),
            )
        ]
    )
    models = ModelRegistry()
    models.register("planner", model)
    planner = ModelTeamPlanner(
        models,
        ModelTeamPlannerConfig("planner", max_tasks=8, max_output_tokens=1234),
    )

    tasks = await planner.plan(
        TeamRequest(
            "交付组件",
            acceptance_criteria=("检查通过",),
            limits=TeamLimits(max_tasks=2),
        )
    )

    assert tuple(task.task_id for task in tasks) == ("design", "implementation")
    assert tasks[1].dependencies == ("design",)
    request = model.requests[0]
    assert request.response_schema_name == "matterloop_team_plan"
    assert request.max_output_tokens == 1234
    assert request.response_schema is not None
    assert request.response_schema["additionalProperties"] is False
    tasks_schema = request.response_schema["properties"]["tasks"]  # type: ignore[index]
    assert tasks_schema["maxItems"] == 2
    assert tasks_schema["items"]["additionalProperties"] is False
    assert "交付组件" in request.messages[1].content


@pytest.mark.parametrize(
    "output, message",
    [
        (
            json.dumps({"tasks": [_task_data("one", "任务")], "extra": True}),
            "fields do not match schema",
        ),
        (
            json.dumps(
                {
                    "tasks": [
                        {
                            **_task_data("one", "任务"),
                            "unexpected": "value",
                        }
                    ]
                }
            ),
            "unexpected",
        ),
        (
            json.dumps(
                {
                    "tasks": [
                        {
                            **_task_data("one", "任务"),
                            "priority": True,
                        }
                    ]
                }
            ),
            "must be an integer",
        ),
        (
            json.dumps(
                {
                    "tasks": [
                        _task_data("one", "任务一", dependencies=("two",)),
                        _task_data("two", "任务二", dependencies=("one",)),
                    ]
                }
            ),
            "invalid DAG",
        ),
    ],
)
async def test_model_team_planner_rejects_non_strict_or_invalid_output(
    output: str,
    message: str,
) -> None:
    """未知字段、类型偷换和无效依赖图都不得进入调度器。"""
    models = ModelRegistry()
    models.register("planner", FakeModelClient([ModelResponse(output_text=output)]))
    planner = ModelTeamPlanner(models, ModelTeamPlannerConfig("planner"))

    with pytest.raises(AgentModelOutputError, match=message):
        await planner.plan(TeamRequest("执行任务"))


async def test_model_team_planner_enforces_effective_limit_and_hot_replacement() -> None:
    """请求限制必须形成硬上限，热替换只影响后续规划调用。"""
    models = ModelRegistry()
    old_model = FakeModelClient([_plan_response(_task_data("old", "旧计划"))])
    models.register("planner", old_model)
    planner = ModelTeamPlanner(models, ModelTeamPlannerConfig("planner", max_tasks=3))

    first = await planner.plan(TeamRequest("目标"))
    replacement = FakeModelClient([_plan_response(_task_data("new", "新计划"))])
    models.register("planner", replacement, replace=True)
    second = await planner.plan(TeamRequest("目标"))

    assert first[0].task_id == "old"
    assert second[0].task_id == "new"
    assert len(old_model.requests) == 1
    assert len(replacement.requests) == 1

    overflowing = FakeModelClient(
        [_plan_response(_task_data("one", "一"), _task_data("two", "二"))]
    )
    models.register("planner", overflowing, replace=True)
    with pytest.raises(AgentModelOutputError, match="limit is 1"):
        await planner.plan(TeamRequest("受限目标", limits=TeamLimits(max_tasks=1)))


async def test_model_team_planner_constrains_capabilities_from_directory_snapshot() -> None:
    """团队规划 Schema 只能声明当前 AgentDirectory 已注册能力。"""
    model = FakeModelClient(
        [_plan_response(_task_data("task", "执行任务", capability="unregistered"))]
    )
    models = ModelRegistry()
    models.register("planner", model)
    planner = ModelTeamPlanner(models, ModelTeamPlannerConfig("planner"))
    context = TeamPlanningContext(
        run_id="team-run",
        request=TeamRequest("受能力约束的目标"),
        cycle=1,
        plan_revision=0,
        available_agents=(AgentSpec("python-agent", frozenset({"python"})),),
    )

    with pytest.raises(AgentModelOutputError, match="not registered"):
        await planner.plan(context)

    request = model.requests[0]
    assert request.response_schema is not None
    tasks_schema = request.response_schema["properties"]["tasks"]  # type: ignore[index]
    capability_schema = tasks_schema["items"]["properties"]["capability"]
    assert capability_schema["enum"] == ["python"]
    assert "python-agent" in request.messages[1].content


async def test_model_task_verifier_is_conservative_and_uses_replaced_model() -> None:
    """验证器必须同时检查执行状态、模型结论、阈值和失败条件。"""
    models = ModelRegistry()
    old_model = FakeModelClient([_verification_response(passed=True, score=79)])
    models.register("verifier", old_model)
    verifier = ModelTaskVerifier(
        models,
        ModelTaskVerifierConfig("verifier", pass_score=80, max_output_tokens=777),
    )
    context = _context()
    result = TaskResult(
        "implementation",
        "python-agent",
        True,
        output="实现完成",
    )

    below_threshold = await verifier.verify(context, result)
    replacement = FakeModelClient([_verification_response(passed=True, score=95)])
    models.register("verifier", replacement, replace=True)
    passed = await verifier.verify(context, result)

    assert below_threshold.passed is False
    assert passed.passed is True
    assert passed.score == 95
    request = replacement.requests[0]
    assert request.response_schema_name == "matterloop_team_task_verification"
    assert request.response_schema is not None
    assert request.response_schema["additionalProperties"] is False
    assert request.max_output_tokens == 777
    assert "实现正确" in request.messages[1].content
    assert "设计完成" in request.messages[1].content

    failed_execution_model = FakeModelClient([_verification_response(passed=True, score=100)])
    models.register("verifier", failed_execution_model, replace=True)
    failed_execution = await verifier.verify(
        context,
        TaskResult(
            "implementation",
            "python-agent",
            False,
            error="执行失败",
        ),
    )
    assert failed_execution.passed is False


async def test_model_task_verifier_rejects_extra_fields_and_failed_criteria() -> None:
    """验证输出必须严格匹配 Schema，失败条件必须覆盖模型的通过声明。"""
    models = ModelRegistry()
    models.register(
        "verifier",
        FakeModelClient(
            [
                _verification_response(
                    passed=True,
                    score=99,
                    failed_criteria=("缺少集成测试",),
                ),
                ModelResponse(
                    output_text=(
                        '{"passed":true,"score":99,"feedback":"完成",'
                        '"evidence":[],"failed_criteria":[],"extra":true}'
                    )
                ),
            ]
        ),
    )
    verifier = ModelTaskVerifier(models, ModelTaskVerifierConfig("verifier"))
    result = TaskResult("implementation", "python-agent", True, output="完成")

    verification = await verifier.verify(_context(), result)
    assert verification.passed is False
    assert verification.failed_criteria == ("缺少集成测试",)
    with pytest.raises(AgentModelOutputError, match="unexpected: extra"):
        await verifier.verify(_context(), result)


async def test_model_task_verifier_accepts_empty_feedback_allowed_by_schema() -> None:
    """通过结果可以使用 Schema 明确允许的空反馈文本。"""
    models = ModelRegistry()
    models.register(
        "verifier",
        FakeModelClient([_verification_response(passed=True, score=100, feedback="")]),
    )
    verifier = ModelTaskVerifier(models, ModelTaskVerifierConfig("verifier"))

    result = await verifier.verify(
        _context(),
        TaskResult("implementation", "python-agent", True, output="完成"),
    )

    assert result.passed is True
    assert result.feedback == ""


async def test_model_result_aggregator_handles_empty_results_and_hot_replacement() -> None:
    """空结果不得触发模型，非空结果应使用调用时最新的注册表客户端。"""
    models = ModelRegistry()
    unused_model = FakeModelClient()
    models.register("aggregator", unused_model)
    aggregator = ModelResultAggregator(
        models,
        ModelResultAggregatorConfig("aggregator", max_output_tokens=888),
    )
    request = TeamRequest("完成发布", acceptance_criteria=("可发布",))

    empty = await aggregator.aggregate(request, ())
    assert empty == ModelResultAggregator.EMPTY_RESULT
    assert unused_model.requests == ()

    first_model = FakeModelClient([ModelResponse(output_text='{"summary":"第一版团队总结"}')])
    models.register("aggregator", first_model, replace=True)
    results = (TaskResult("release", "release-agent", True, output="发布完成"),)
    first = await aggregator.aggregate(request, results)
    replacement = FakeModelClient([ModelResponse(output_text='{"summary":"替换后的团队总结"}')])
    models.register("aggregator", replacement, replace=True)
    second = await aggregator.aggregate(request, results)

    assert first == "第一版团队总结"
    assert second == "替换后的团队总结"
    model_request = replacement.requests[0]
    assert model_request.response_schema_name == "matterloop_team_result"
    assert model_request.response_schema is not None
    assert model_request.response_schema["additionalProperties"] is False
    assert model_request.max_output_tokens == 888
    assert "发布完成" in model_request.messages[1].content


async def test_model_result_aggregator_rejects_empty_or_extra_summary_fields() -> None:
    """聚合器不得接受空总结或 Schema 之外的模型字段。"""
    models = ModelRegistry()
    models.register(
        "aggregator",
        FakeModelClient(
            [
                ModelResponse(output_text='{"summary":""}'),
                ModelResponse(output_text='{"summary":"完成","extra":true}'),
            ]
        ),
    )
    aggregator = ModelResultAggregator(models, ModelResultAggregatorConfig("aggregator"))
    request = TeamRequest("目标")
    results = (TaskResult("task", "agent", True, output="完成"),)

    with pytest.raises(AgentModelOutputError, match="non-empty string"):
        await aggregator.aggregate(request, results)
    with pytest.raises(AgentModelOutputError, match="unexpected: extra"):
        await aggregator.aggregate(request, results)


async def test_model_team_reviewer_requests_human_and_applies_local_pass_threshold() -> None:
    """团队审查器应产生结构化人工请求，并在本地执行通过阈值。"""
    responses = (
        ModelResponse(
            output_text=json.dumps(
                {
                    "action": "request_human",
                    "score": 85,
                    "feedback": "需要业务确认",
                    "evidence": ["任务证据"],
                    "failed_criteria": [],
                    "human_prompt": "请确认是否接受草稿",
                },
                ensure_ascii=False,
            )
        ),
        ModelResponse(
            output_text=json.dumps(
                {
                    "action": "accept",
                    "score": 70,
                    "feedback": "分数不足",
                    "evidence": [],
                    "failed_criteria": [],
                    "human_prompt": "",
                },
                ensure_ascii=False,
            )
        ),
    )
    model = FakeModelClient(responses)
    models = ModelRegistry()
    models.register("reviewer", model)
    reviewer = ModelTeamReviewer(
        models,
        ModelTeamReviewerConfig("reviewer", pass_score=80),
    )
    context = TeamReviewContext(
        run_id="team-run",
        request=TeamRequest("验收团队草稿"),
        cycle=1,
        plan_revision=0,
        task_results=(TaskResult("task", "agent", True, output="完成"),),
        draft_output="团队草稿",
    )

    human_review = await reviewer.review(context)
    below_threshold = await reviewer.review(context)

    assert human_review.action is TeamReviewAction.REQUEST_HUMAN
    assert human_review.interaction is not None
    assert human_review.interaction.prompt == "请确认是否接受草稿"
    assert below_threshold.action is TeamReviewAction.REPLAN
    assert model.requests[0].response_schema_name == "matterloop_team_review"
