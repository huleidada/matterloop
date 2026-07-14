"""验收验证器、审查器和适配器测试。"""

from __future__ import annotations

import asyncio

from matterloop_agents import (
    CriteriaVerifier,
    CriteriaVerifierConfig,
    ModelReviewer,
    ModelReviewerConfig,
    ReviewerVerifierAdapter,
)
from matterloop_core import ExecutionResult, LoopContext, LoopRequest, PlanStep
from matterloop_models import (
    FakeModelClient,
    ModelClient,
    ModelLease,
    ModelRegistry,
    ModelResponse,
)


class AcquireTrackingRegistry(ModelRegistry):
    """记录验证类 Agent 的模型租约，并禁止回退到直接查询。"""

    def __init__(self) -> None:
        super().__init__()
        self.acquired: list[str] = []

    def acquire(self, name: str) -> ModelLease:
        """记录并返回查询时刻固定的模型客户端。"""
        self.acquired.append(name)
        return super().acquire(name)

    def get(self, name: str) -> ModelClient:
        """禁止 Agent 绕过事务租约直接查询客户端。"""
        raise AssertionError(f"verification agent used ModelRegistry.get({name!r})")


def test_criteria_verifier_requires_score_and_no_failed_criteria() -> None:
    async def scenario() -> None:
        models = AcquireTrackingRegistry()
        models.register(
            "verifier",
            FakeModelClient(
                [
                    ModelResponse(
                        output_text=(
                            '{"passed":true,"score":95,"feedback":"仍缺测试",'
                            '"evidence":["输出存在"],"failed_criteria":["测试通过"]}'
                        )
                    )
                ]
            ),
        )
        verifier = CriteriaVerifier(models, CriteriaVerifierConfig(model="verifier"))

        verification = await verifier.verify(
            PlanStep(description="实现功能", acceptance_criteria=("测试通过",)),
            ExecutionResult(output="已实现"),
            LoopContext(LoopRequest(goal="交付功能")),
        )

        assert not verification.passed
        assert verification.score == 95
        assert verification.failed_criteria == ("测试通过",)
        assert models.acquired == ["verifier"]

    asyncio.run(scenario())


def test_reviewer_adapter_converts_issues_to_failed_verification() -> None:
    async def scenario() -> None:
        models = AcquireTrackingRegistry()
        models.register(
            "reviewer",
            FakeModelClient(
                [
                    ModelResponse(
                        output_text=(
                            '{"score":88,"summary":"存在风险","evidence":["日志"],'
                            '"issues":["未处理超时"],"recommendations":["增加超时"]}'
                        )
                    )
                ]
            ),
        )
        adapter = ReviewerVerifierAdapter(
            ModelReviewer(models, ModelReviewerConfig(model="reviewer"))
        )

        verification = await adapter.verify(
            PlanStep(description="实现接口"),
            ExecutionResult(output="完成"),
            LoopContext(LoopRequest(goal="实现接口")),
        )

        assert not verification.passed
        assert verification.failed_criteria == ("未处理超时",)
        assert verification.evidence == ("日志",)
        assert models.acquired == ["reviewer"]

    asyncio.run(scenario())
