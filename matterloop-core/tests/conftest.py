"""供核心模块测试复用的零第三方运行时依赖测试组件。"""

from matterloop_core import (
    AgentLoop,
    ApprovalDecision,
    CheckpointConflictError,
    ComponentRegistry,
    ExecutionResult,
    Executor,
    LocalEventPublisher,
    LoopContext,
    Plan,
    Planner,
    PlanStep,
    RetryAction,
    RetryDecision,
    VerificationResult,
    Verifier,
)


class GoalPlanner:
    """根据当前目标生成一个无需审批的步骤。"""

    async def plan(self, context: LoopContext) -> Plan:
        """创建一个确定性测试计划。"""
        return Plan((PlanStep(context.request.goal),))


class EchoExecutor:
    """把步骤描述作为确定性输出返回。"""

    async def execute(self, step: PlanStep, context: LoopContext) -> ExecutionResult:
        """原样返回选中的步骤描述。"""
        del context
        return ExecutionResult(step.description)


class PassingVerifier:
    """通过所有确定性的测试执行结果。"""

    async def verify(
        self,
        step: PlanStep,
        result: ExecutionResult,
        context: LoopContext,
    ) -> VerificationResult:
        """返回带证据的验证成功结论。"""
        del step, result, context
        return VerificationResult(True, score=100, evidence=("测试验证通过",))


class AlwaysContinuePolicy:
    """允许测试运行继续，预算由内核自身负责。"""

    def can_continue(self, context: LoopContext) -> bool:
        """始终允许进入下一个安全边界。"""
        del context
        return True


class ApproveAllGate:
    """批准被显式标记为需要审批的测试步骤。"""

    async def decide(self, step: PlanStep, context: LoopContext) -> ApprovalDecision:
        """返回批准决策。"""
        del step, context
        return ApprovalDecision.APPROVED


class FailFastRetry:
    """在未覆盖策略的测试中立即暴露组件异常。"""

    def decide(self, error: Exception, attempt: int, context: LoopContext) -> RetryDecision:
        """返回不重试决策。"""
        del error, attempt, context
        return RetryDecision(RetryAction.FAIL)


class MemoryCheckpointStore:
    """在内存中保存供核心测试使用的隔离检查点。"""

    def __init__(self) -> None:
        self.contexts: dict[str, LoopContext] = {}

    async def save(
        self,
        context: LoopContext,
        *,
        expected_revision: int | None = None,
    ) -> int:
        """使用 revision 比较并保存上下文快照。"""
        expected = context.revision if expected_revision is None else expected_revision
        current = self.contexts.get(context.run_id)
        current_revision = current.revision if current is not None else 0
        if current_revision != expected:
            raise CheckpointConflictError("checkpoint revision conflict")
        revision = expected + 1
        snapshot = context.snapshot()
        snapshot.revision = revision
        self.contexts[context.run_id] = snapshot
        return revision

    async def load(self, run_id: str) -> LoopContext | None:
        """当上下文快照存在时将其加载。"""
        context = self.contexts.get(run_id)
        return context.snapshot() if context is not None else None


def build_loop() -> tuple[AgentLoop, MemoryCheckpointStore, LocalEventPublisher]:
    """使用测试替身组装一个完整核心 Loop。"""
    planners = ComponentRegistry[Planner]()
    executors = ComponentRegistry[Executor]()
    verifiers = ComponentRegistry[Verifier]()
    planners.register("default", GoalPlanner())
    executors.register("default", EchoExecutor())
    verifiers.register("default", PassingVerifier())
    store = MemoryCheckpointStore()
    events = LocalEventPublisher()
    return (
        AgentLoop(
            planners=planners,
            executors=executors,
            verifiers=verifiers,
            checkpoint_store=store,
            policy=AlwaysContinuePolicy(),
            events=events,
            approval_gate=ApproveAllGate(),
            retry_policy=FailFastRetry(),
        ),
        store,
        events,
    )
