"""供 MatterLoop 可选模块实现的结构化扩展协议。"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from matterloop_core.context import (
    ExecutionResult,
    LoopContext,
    Plan,
    PlanStep,
    VerificationResult,
)
from matterloop_core.control import ApprovalDecision, CompletionDecision, RetryDecision
from matterloop_core.events import LoopEvent


@runtime_checkable
class Planner(Protocol):
    """根据当前 Loop 状态创建或修订可执行计划。"""

    async def plan(self, context: LoopContext) -> Plan:
        """生成下一轮计划。

        Args:
            context: 包含验证反馈的当前运行状态。

        Returns:
            下一轮需要执行的有序工作步骤。
        """
        ...


@runtime_checkable
class Executor(Protocol):
    """执行计划步骤，但不判断执行结果是否正确。"""

    async def execute(self, step: PlanStep, context: LoopContext) -> ExecutionResult:
        """执行一个步骤并返回可观察的结果证据。"""
        ...


@runtime_checkable
class Verifier(Protocol):
    """根据明确的验收条件独立评估执行结果。"""

    async def verify(
        self,
        step: PlanStep,
        result: ExecutionResult,
        context: LoopContext,
    ) -> VerificationResult:
        """返回独立验证结论和可执行反馈。"""
        ...


@runtime_checkable
class CheckpointStore(Protocol):
    """持久化 Loop 检查点，同时避免内核与具体数据库耦合。"""

    async def save(self, context: LoopContext, *, expected_revision: int | None = None) -> int:
        """使用 CAS 持久化检查点并返回提交后的 revision。

        ``expected_revision`` 省略时使用 ``context.revision``，便于直接保存新上下文。
        实现必须原子比较当前 revision，冲突时抛出 ``CheckpointConflictError``。
        """
        ...

    async def load(self, run_id: str) -> LoopContext | None:
        """当指定检查点存在时将其加载。"""
        ...


@runtime_checkable
class LoopPolicy(Protocol):
    """在安全边界应用调用方组合的业务停止规则。"""

    def can_continue(self, context: LoopContext) -> bool:
        """判断是否允许开始下一次迭代。"""
        ...


@runtime_checkable
class EventPublisher(Protocol):
    """将生命周期事件转发给可观测性模块和集成模块。"""

    async def publish(self, event: LoopEvent) -> None:
        """发布一个不可变生命周期事件。"""
        ...


@runtime_checkable
class ApprovalGate(Protocol):
    """在具有外部影响的步骤执行前给出审批决策。"""

    async def decide(self, step: PlanStep, context: LoopContext) -> ApprovalDecision:
        """返回批准、拒绝或延期决策。"""
        ...


@runtime_checkable
class RetryPolicy(Protocol):
    """决定组件调用异常后是重试、重新规划还是失败。"""

    def decide(self, error: Exception, attempt: int, context: LoopContext) -> RetryDecision:
        """根据异常、尝试次数与上下文返回处理决策。"""
        ...


@runtime_checkable
class CompletionEvaluator(Protocol):
    """在所有步骤通过后独立验收整个 Loop 目标。"""

    async def evaluate(self, context: LoopContext) -> CompletionDecision:
        """返回接受、重新规划、人工复核或停止决策。"""
        ...
