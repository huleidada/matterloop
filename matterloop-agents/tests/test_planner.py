"""模型规划器和长期记忆注入测试。"""

from __future__ import annotations

import asyncio

import pytest
from matterloop_agents import ModelPlanner, ModelPlannerConfig, PlanStepLimitError
from matterloop_core import LoopContext, LoopRequest
from matterloop_memory import InMemoryMemoryStore, MemoryKind, MemoryRecord
from matterloop_models import (
    FakeModelClient,
    ModelClient,
    ModelLease,
    ModelRegistry,
    ModelRequest,
    ModelResponse,
)


class AcquireTrackingRegistry(ModelRegistry):
    """记录租约获取，并在 Agent 回退使用旧 ``get`` API 时立即失败。"""

    def __init__(self) -> None:
        super().__init__()
        self.acquired: list[str] = []

    def acquire(self, name: str) -> ModelLease:
        """记录一次模型事务并返回真实租约。"""
        self.acquired.append(name)
        return super().acquire(name)

    def get(self, name: str) -> ModelClient:
        """禁止规划器绕过租约直接查询客户端。"""
        raise AssertionError(f"planner used ModelRegistry.get({name!r})")


def test_planner_builds_typed_plan_and_injects_memory() -> None:
    async def scenario() -> None:
        memory = InMemoryMemoryStore()
        await memory.put(
            MemoryRecord(
                namespace="project",
                kind=MemoryKind.PROCEDURAL,
                content="先运行静态检查",
            )
        )

        def respond(request: ModelRequest) -> ModelResponse:
            # FakeModelClient 已记录强类型请求，这里只需验证提示词包含检索结果。
            assert "先运行静态检查" in request.messages[1].content
            return ModelResponse(
                output_text=(
                    '{"steps":[{"description":"运行检查","executor":"coding",'
                    '"acceptance_criteria":["检查通过"],"requires_approval":false}]}'
                )
            )

        models = ModelRegistry()
        models.register("planner", FakeModelClient(responder=respond))
        planner = ModelPlanner(
            models,
            ModelPlannerConfig(model="planner", memory_namespace="project"),
            memory=memory,
        )

        plan = await planner.plan(LoopContext(LoopRequest(goal="修复代码")))

        assert len(plan.steps) == 1
        assert plan.steps[0].description == "运行检查"
        assert plan.steps[0].executor == "coding"
        assert plan.steps[0].acceptance_criteria == ("检查通过",)

    asyncio.run(scenario())


def test_planner_resolves_replaced_model_on_each_call() -> None:
    async def scenario() -> None:
        models = AcquireTrackingRegistry()
        models.register(
            "planner",
            FakeModelClient(
                [
                    ModelResponse(
                        output_text=(
                            '{"steps":[{"description":"旧计划","executor":"default",'
                            '"acceptance_criteria":[],"requires_approval":false}]}'
                        )
                    )
                ]
            ),
        )
        planner = ModelPlanner(models, ModelPlannerConfig(model="planner"))
        context = LoopContext(LoopRequest(goal="目标"))
        first = await planner.plan(context)

        models.register(
            "planner",
            FakeModelClient(
                [
                    ModelResponse(
                        output_text=(
                            '{"steps":[{"description":"新计划","executor":"default",'
                            '"acceptance_criteria":[],"requires_approval":false}]}'
                        )
                    )
                ]
            ),
            replace=True,
        )
        second = await planner.plan(context)

        assert first.steps[0].description == "旧计划"
        assert second.steps[0].description == "新计划"
        assert models.acquired == ["planner", "planner"]

    asyncio.run(scenario())


def test_planner_rejects_plan_over_configured_step_limit() -> None:
    async def scenario() -> None:
        models = ModelRegistry()
        models.register(
            "planner",
            FakeModelClient(
                [
                    ModelResponse(
                        output_text=(
                            '{"steps":['
                            '{"description":"一","executor":"default",'
                            '"acceptance_criteria":[],"requires_approval":false},'
                            '{"description":"二","executor":"default",'
                            '"acceptance_criteria":[],"requires_approval":false}'
                            "]}"
                        )
                    )
                ]
            ),
        )
        planner = ModelPlanner(models, ModelPlannerConfig(model="planner", max_steps=1))

        with pytest.raises(PlanStepLimitError):
            await planner.plan(LoopContext(LoopRequest(goal="目标")))

    asyncio.run(scenario())
