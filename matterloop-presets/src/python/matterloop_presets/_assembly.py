"""预设之间共享的 Agent、策略和注册表装配细节。"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace
from typing import cast

from matterloop_agents import (
    CriteriaVerifier,
    CriteriaVerifierConfig,
    ModelPlanner,
    ModelPlannerConfig,
    ToolCallingWorker,
    ToolCallingWorkerConfig,
)
from matterloop_core import (
    AgentLoop,
    ApprovalGate,
    CheckpointStore,
    ComponentRegistry,
    EventPublisher,
    ExecutionResult,
    Executor,
    LoopContext,
    Plan,
    Planner,
    PlanStep,
    VerificationResult,
    Verifier,
)
from matterloop_models import ModelClient, ModelRegistry
from matterloop_policies import (
    CompositeLoopPolicy,
    ExponentialBackoffRetryPolicy,
    NoProgressStopPolicy,
    StopConfig,
)
from matterloop_runtime import AsyncClosable
from matterloop_tools import ToolRegistry

from matterloop_presets.config import AgentPresetConfig
from matterloop_presets.errors import PresetConfigurationError
from matterloop_presets.runtime import PresetRuntime


class _ApprovalEnforcingPlanner:
    """强制高权限执行器的全部步骤先进入核心审批门。"""

    def __init__(self, planner: Planner, privileged_executors: frozenset[str]) -> None:
        self._planner = planner
        self._privileged_executors = privileged_executors

    async def plan(self, context: LoopContext) -> Plan:
        """保留计划内容，只提升高权限步骤的审批要求。"""
        plan = await self._planner.plan(context)
        return Plan(
            tuple(
                replace(
                    step,
                    requires_approval=(
                        step.requires_approval or step.executor in self._privileged_executors
                    ),
                )
                for step in plan.steps
            )
        )


class _CitationRequiredVerifier:
    """在模型验证后增加研究结果的引用证据硬门槛。"""

    def __init__(self, verifier: Verifier) -> None:
        self._verifier = verifier

    async def verify(
        self,
        step: PlanStep,
        result: ExecutionResult,
        context: LoopContext,
    ) -> VerificationResult:
        """没有验证证据或执行制品引用时，把通过结论降级为失败。"""
        verification = await self._verifier.verify(step, result, context)
        if not verification.passed or _has_citation(verification.evidence, result):
            return verification
        return replace(
            verification,
            passed=False,
            feedback="研究结果缺少可追溯引用证据",
            failed_criteria=("引用证据",),
        )


def _assemble_runtime(
    *,
    model: ModelClient,
    config: AgentPresetConfig,
    checkpoint_store: CheckpointStore,
    events: EventPublisher,
    approval_gate: ApprovalGate,
    tool_registries: Mapping[str, ToolRegistry],
    executor_tools: Mapping[str, tuple[str, ...]],
    privileged_executors: frozenset[str] = frozenset(),
    require_citation: bool = False,
    extra_resources: tuple[AsyncClosable, ...] = (),
) -> PresetRuntime:
    """把稳定组件协议装配为一个异步运行门面。"""
    models = ModelRegistry()
    models.register(config.model_name, model)

    planner: Planner = ModelPlanner(
        models,
        ModelPlannerConfig(
            model=config.model_name,
            default_executor="default",
            max_steps=config.max_plan_steps,
        ),
    )
    if privileged_executors:
        planner = _ApprovalEnforcingPlanner(planner, privileged_executors)

    verifier: Verifier = CriteriaVerifier(
        models,
        CriteriaVerifierConfig(model=config.model_name, pass_score=config.pass_score),
    )
    if require_citation:
        verifier = _CitationRequiredVerifier(verifier)

    planners = ComponentRegistry[Planner]()
    executors = ComponentRegistry[Executor]()
    verifiers = ComponentRegistry[Verifier]()
    planners.register("default", planner)
    for executor_name, tool_names in executor_tools.items():
        tools = tool_registries.get(executor_name)
        if tools is None:
            raise PresetConfigurationError(
                f"executor {executor_name!r} has no matching tool registry"
            )
        executors.register(
            executor_name,
            ToolCallingWorker(
                models,
                tools,
                ToolCallingWorkerConfig(
                    model=config.model_name,
                    tool_names=tool_names,
                    max_tool_rounds=config.max_tool_rounds,
                ),
            ),
        )
    verifiers.register("default", verifier)

    loop = AgentLoop(
        planners=planners,
        executors=executors,
        verifiers=verifiers,
        checkpoint_store=checkpoint_store,
        policy=CompositeLoopPolicy(NoProgressStopPolicy(StopConfig(config.max_identical_feedback))),
        events=events,
        approval_gate=approval_gate,
        retry_policy=ExponentialBackoffRetryPolicy(config.retry),
    )
    resources: list[AsyncClosable] = []
    if callable(getattr(model, "aclose", None)):
        resources.append(cast(AsyncClosable, model))
    resources.extend(tool_registries.values())
    resources.extend(extra_resources)
    return PresetRuntime(
        loop,
        models,
        tool_registries,
        checkpoint_store,
        config,
        resources=tuple(resources),
    )


def _has_citation(evidence: tuple[str, ...], result: ExecutionResult) -> bool:
    """判断验证证据或执行制品是否包含可追溯引用。"""
    prefixes = ("http://", "https://", "artifact://")
    if any(any(prefix in item.casefold() for prefix in prefixes) for item in evidence):
        return True
    return any(artifact.uri.casefold().startswith(prefixes) for artifact in result.artifacts)


__all__: list[str] = []
