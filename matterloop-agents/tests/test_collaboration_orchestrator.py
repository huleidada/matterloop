"""多 Agent 团队编排、恢复、取消和 Loop 适配测试。"""

from __future__ import annotations

import asyncio
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field

import matterloop_agents.collaboration.runtime as team_runtime_module
import pytest
from matterloop_agents.collaboration import (
    AgentDirectory,
    AgentSpec,
    AgentTaskContext,
    AlwaysApproveTeamGate,
    AsyncTeamRuntime,
    ConcatenateResultAggregator,
    InMemoryTeamRepository,
    LeastBusyScheduler,
    LocalTeamEventPublisher,
    LocalTeamRuntime,
    LoopAgentEndpoint,
    ResultSuccessVerifier,
    StaticTeamPlanner,
    TaskResult,
    TaskSpec,
    TaskState,
    TaskStatus,
    TaskVerification,
    TaskVerifier,
    TeamApprovalGate,
    TeamEvent,
    TeamEventType,
    TeamLimits,
    TeamOrchestrator,
    TeamOrchestratorComponents,
    TeamRequest,
    TeamResult,
    TeamRunActiveError,
    TeamSnapshot,
    TeamStatus,
    TeamStopReason,
)
from matterloop_core import (
    ApprovalDecision,
    HumanAction,
    HumanResponse,
    LoopResult,
    LoopStatus,
    StopReason,
)


@dataclass(slots=True)
class _Endpoint:
    spec: AgentSpec
    calls: list[AgentTaskContext] = field(default_factory=list)
    started: asyncio.Event | None = None
    release: asyncio.Event | None = None

    async def execute(self, context: AgentTaskContext) -> TaskResult:
        """记录上下文并返回与当前尝试关联的成功结果。"""
        self.calls.append(context)
        if self.started is not None:
            self.started.set()
        if self.release is not None:
            await self.release.wait()
        dependency_output = "+".join(item.output for item in context.dependency_results)
        output = dependency_output or f"{context.task.task_id}:{context.attempt}"
        return TaskResult(
            task_id=context.task.task_id,
            agent_id=context.agent_id,
            success=True,
            output=output,
            attempt=context.attempt,
        )


def _endpoint(
    agent_id: str,
    capability: str,
    *,
    started: asyncio.Event | None = None,
    release: asyncio.Event | None = None,
) -> _Endpoint:
    """构造单能力测试端点。"""
    return _Endpoint(
        AgentSpec(agent_id=agent_id, capabilities=frozenset({capability})),
        started=started,
        release=release,
    )


def _orchestrator(
    tasks: tuple[TaskSpec, ...],
    endpoints: tuple[_Endpoint, ...],
    *,
    verifier: TaskVerifier | None = None,
    approval_gate: TeamApprovalGate | None = None,
    repository: InMemoryTeamRepository | None = None,
    events: LocalTeamEventPublisher | None = None,
) -> TeamOrchestrator:
    """用完全显式注入的内存组件装配测试控制器。"""
    directory = AgentDirectory()
    for endpoint in endpoints:
        directory.register(endpoint)
    return TeamOrchestrator(
        TeamOrchestratorComponents(
            planner=StaticTeamPlanner(tasks),
            agents=directory,
            selection_policy=LeastBusyScheduler(),
            verifier=verifier or ResultSuccessVerifier(),
            approval_gate=approval_gate or AlwaysApproveTeamGate(),
            repository=repository or InMemoryTeamRepository(),
            events=events or LocalTeamEventPublisher(),
            aggregator=ConcatenateResultAggregator(),
        )
    )


async def test_team_orchestrator_executes_fan_out_and_fan_in_by_capability() -> None:
    """独立任务应并行执行，汇总任务只能在全部依赖成功后启动。"""
    both_started = asyncio.Event()
    start_count = 0

    class ParallelEndpoint(_Endpoint):
        async def execute(self, context: AgentTaskContext) -> TaskResult:
            nonlocal start_count
            start_count += 1
            if start_count == 2:
                both_started.set()
            await asyncio.wait_for(both_started.wait(), timeout=1)
            return await super().execute(context)

    research = ParallelEndpoint(
        AgentSpec("researcher", frozenset({"research"})),
    )
    coding = ParallelEndpoint(AgentSpec("coder", frozenset({"coding"})))
    reviewer = _endpoint("reviewer", "review")
    tasks = (
        TaskSpec("research", "调研方案", "research"),
        TaskSpec("coding", "实现方案", "coding"),
        TaskSpec(
            "review",
            "综合审查",
            "review",
            dependencies=("research", "coding"),
        ),
    )

    result = await _orchestrator(tasks, (research, coding, reviewer)).run(
        TeamRequest("完成调研、实现和审查")
    )

    assert result.status is TeamStatus.COMPLETED
    assert result.stop_reason is TeamStopReason.COMPLETED
    assert start_count == 2
    assert len(reviewer.calls) == 1
    assert tuple(item.task_id for item in reviewer.calls[0].dependency_results) == (
        "research",
        "coding",
    )
    assert result.completed_tasks == 3


async def test_failed_verification_retries_without_replanning() -> None:
    """验证失败应重试同一任务，并保留单调递增的尝试次数。"""

    class RetryVerifier:
        def __init__(self) -> None:
            self.calls = 0

        async def verify(
            self,
            context: AgentTaskContext,
            result: TaskResult,
        ) -> TaskVerification:
            del context, result
            self.calls += 1
            return TaskVerification(
                passed=self.calls > 1,
                feedback="首次结果证据不足" if self.calls == 1 else "",
            )

    verifier = RetryVerifier()
    worker = _endpoint("worker", "python")
    events = LocalTeamEventPublisher()
    published: list[TeamEvent] = []
    events.subscribe(published.append)
    orchestrator = _orchestrator(
        (TaskSpec("implement", "实现功能", "python"),),
        (worker,),
        verifier=verifier,
        events=events,
    )

    result = await orchestrator.run(TeamRequest("实现功能", limits=TeamLimits(max_task_attempts=2)))

    assert result.status is TeamStatus.COMPLETED
    assert [context.attempt for context in worker.calls] == [1, 2]
    assert verifier.calls == 2
    assert TeamEventType.TASK_RETRYING in {event.event_type for event in published}


async def test_deferred_approval_pauses_and_resume_continues_exact_task() -> None:
    """审批暂缓必须持久化当前节点，恢复后不重新规划或丢失尝试计数。"""

    class DeferredOnceGate:
        def __init__(self) -> None:
            self.calls = 0

        async def decide(self, context: AgentTaskContext) -> ApprovalDecision:
            del context
            self.calls += 1
            if self.calls == 1:
                return ApprovalDecision.DEFERRED
            return ApprovalDecision.APPROVED

    repository = InMemoryTeamRepository()
    gate = DeferredOnceGate()
    worker = _endpoint("writer", "filesystem-write")
    orchestrator = _orchestrator(
        (
            TaskSpec(
                "write",
                "写入文件",
                "filesystem-write",
                requires_approval=True,
            ),
        ),
        (worker,),
        approval_gate=gate,
        repository=repository,
    )

    paused = await orchestrator.run(TeamRequest("生成文件"), run_id="approval-run")
    persisted = await repository.require("approval-run")
    assert paused.pending_interaction is not None
    await orchestrator.submit_human_response(
        "approval-run",
        HumanResponse(
            interaction_id=paused.pending_interaction.interaction_id,
            action=HumanAction.APPROVE,
            idempotency_key="approve-write",
        ),
    )
    resumed = await orchestrator.resume("approval-run")

    assert paused.status is TeamStatus.PAUSED
    assert paused.stop_reason is TeamStopReason.APPROVAL_DEFERRED
    assert persisted.tasks[0].status is TaskStatus.WAITING_APPROVAL
    assert persisted.tasks[0].attempt == 0
    assert resumed.status is TeamStatus.COMPLETED
    assert worker.calls[0].attempt == 1
    assert gate.calls == 1


async def test_blocked_missing_capability_can_resume_after_hot_registration() -> None:
    """缺少能力时应阻塞而非破坏任务，注册新 Agent 后可以恢复。"""
    directory = AgentDirectory()
    repository = InMemoryTeamRepository()
    components = TeamOrchestratorComponents(
        planner=StaticTeamPlanner((TaskSpec("audit", "安全审计", "security"),)),
        agents=directory,
        selection_policy=LeastBusyScheduler(),
        verifier=ResultSuccessVerifier(),
        approval_gate=AlwaysApproveTeamGate(),
        repository=repository,
        events=LocalTeamEventPublisher(),
        aggregator=ConcatenateResultAggregator(),
    )
    orchestrator = TeamOrchestrator(components)

    blocked = await orchestrator.run(TeamRequest("完成安全审计"), run_id="hot-run")
    security_agent = _endpoint("security-agent", "security")
    directory.register(security_agent)
    resumed = await orchestrator.resume("hot-run")

    assert blocked.status is TeamStatus.BLOCKED
    assert blocked.stop_reason is TeamStopReason.NO_CAPABLE_AGENT
    assert resumed.status is TeamStatus.COMPLETED
    assert len(security_agent.calls) == 1


async def test_active_run_cancels_at_batch_boundary() -> None:
    """运行中取消应等待端点归还租约，并提交一致的取消快照。"""
    started = asyncio.Event()
    release = asyncio.Event()
    worker = _endpoint("worker", "slow", started=started, release=release)
    orchestrator = _orchestrator(
        (TaskSpec("slow-task", "执行慢任务", "slow"),),
        (worker,),
    )

    running = asyncio.create_task(orchestrator.run(TeamRequest("执行慢任务"), run_id="cancel-run"))
    await asyncio.wait_for(started.wait(), timeout=1)
    assert await orchestrator.cancel("cancel-run") is True
    assert await orchestrator.cancel("cancel-run") is False
    release.set()
    result = await running

    assert result.status is TeamStatus.CANCELLED
    assert result.stop_reason is TeamStopReason.CANCELLED


async def test_team_timeout_cancels_inflight_task_and_releases_agent_capacity() -> None:
    """总超时应取消批次、保存超时状态，并允许同一 Agent 继续接新运行。"""
    first_started = asyncio.Event()
    never_release = asyncio.Event()
    worker = _endpoint(
        "worker",
        "slow",
        started=first_started,
        release=never_release,
    )
    orchestrator = _orchestrator(
        (TaskSpec("slow-task", "执行慢任务", "slow"),),
        (worker,),
    )

    timed_out = await orchestrator.run(
        TeamRequest("执行慢任务", limits=TeamLimits(timeout_seconds=0.01))
    )
    worker.release = None
    completed = await orchestrator.run(TeamRequest("再次执行慢任务"))

    assert first_started.is_set()
    assert timed_out.status is TeamStatus.TIMED_OUT
    assert timed_out.stop_reason is TeamStopReason.TIMED_OUT
    assert completed.status is TeamStatus.COMPLETED


async def test_external_coroutine_cancellation_persists_cancelled_snapshot() -> None:
    """调用方取消协程时不得遗留无法恢复的 RUNNING 快照。"""
    started = asyncio.Event()
    never_release = asyncio.Event()
    repository = InMemoryTeamRepository()
    worker = _endpoint(
        "worker",
        "slow",
        started=started,
        release=never_release,
    )
    orchestrator = _orchestrator(
        (TaskSpec("slow-task", "执行慢任务", "slow"),),
        (worker,),
        repository=repository,
    )
    running = asyncio.create_task(
        orchestrator.run(TeamRequest("执行慢任务"), run_id="external-cancel")
    )
    await asyncio.wait_for(started.wait(), timeout=1)

    running.cancel()
    with pytest.raises(asyncio.CancelledError):
        await running
    persisted = await repository.require("external-cancel")

    assert persisted.status is TeamStatus.CANCELLED
    assert persisted.stop_reason is TeamStopReason.CANCELLED
    assert persisted.tasks[0].status is TaskStatus.CANCELLED


async def test_resume_recovers_orphaned_running_task_without_replanning() -> None:
    """进程崩溃留下的 RUNNING 节点应归还租约并从下一次尝试恢复。"""
    repository = InMemoryTeamRepository()
    task = TaskSpec("task", "恢复执行", "python")
    await repository.create(
        TeamSnapshot(
            TeamRequest("恢复运行"),
            (
                TaskState(
                    task,
                    status=TaskStatus.RUNNING,
                    attempt=1,
                    assigned_agent="crashed-worker",
                ),
            ),
            run_id="orphaned-running",
            status=TeamStatus.RUNNING,
        )
    )
    worker = _endpoint("replacement-worker", "python")
    orchestrator = _orchestrator((task,), (worker,), repository=repository)

    result = await orchestrator.resume("orphaned-running")

    assert result.status is TeamStatus.COMPLETED
    assert worker.calls[0].attempt == 2
    assert worker.calls[0].previous_error == "recovered from interrupted execution"


async def test_resume_and_cancel_support_empty_orphaned_planning_snapshot() -> None:
    """规划阶段崩溃产生的空任务快照必须可以重规划或直接取消。"""
    repository = InMemoryTeamRepository()
    task = TaskSpec("task", "重新规划后执行", "python")
    request = TeamRequest("恢复规划")
    for run_id in ("resume-planning", "cancel-planning"):
        await repository.create(
            TeamSnapshot(
                request,
                (),
                run_id=run_id,
                status=TeamStatus.PLANNING,
            )
        )
    worker = _endpoint("worker", "python")
    orchestrator = _orchestrator((task,), (worker,), repository=repository)

    resumed = await orchestrator.resume("resume-planning")
    cancelled = await orchestrator.cancel("cancel-planning")
    cancelled_snapshot = await repository.require("cancel-planning")

    assert resumed.status is TeamStatus.COMPLETED
    assert cancelled is True
    assert cancelled_snapshot.status is TeamStatus.CANCELLED
    assert cancelled_snapshot.tasks == ()


async def test_concurrent_resume_uses_cross_orchestrator_run_lease() -> None:
    """共享仓储的两个控制器不能同时执行同一个恢复任务。"""
    repository = InMemoryTeamRepository()
    task = TaskSpec("task", "竞争恢复", "python")
    await repository.create(
        TeamSnapshot(
            TeamRequest("竞争恢复"),
            (TaskState(task, status=TaskStatus.READY),),
            run_id="concurrent-resume",
            status=TeamStatus.RUNNING,
        )
    )
    started = asyncio.Event()
    release = asyncio.Event()
    first_worker = _endpoint("first", "python", started=started, release=release)
    second_worker = _endpoint("second", "python")
    first = _orchestrator((task,), (first_worker,), repository=repository)
    second = _orchestrator((task,), (second_worker,), repository=repository)

    winner = asyncio.create_task(first.resume("concurrent-resume"))
    await asyncio.wait_for(started.wait(), timeout=1)
    with pytest.raises(TeamRunActiveError):
        await second.resume("concurrent-resume")
    release.set()
    result = await winner
    persisted = await repository.require("concurrent-resume")

    assert isinstance(result, TeamResult)
    assert persisted.status is TeamStatus.COMPLETED
    assert persisted.stop_reason is TeamStopReason.COMPLETED
    assert len(first_worker.calls) == 1
    assert second_worker.calls == []


async def test_component_timeout_error_is_not_misreported_as_team_deadline() -> None:
    """组件主动抛出的 TimeoutError 应是组件失败，不是团队总时限耗尽。"""

    class TimeoutPlanner:
        async def plan(self, request: TeamRequest) -> tuple[TaskSpec, ...]:
            del request
            raise asyncio.TimeoutError("planner backend timed out")

    directory = AgentDirectory()
    orchestrator = TeamOrchestrator(
        TeamOrchestratorComponents(
            planner=TimeoutPlanner(),
            agents=directory,
            selection_policy=LeastBusyScheduler(),
            verifier=ResultSuccessVerifier(),
            approval_gate=AlwaysApproveTeamGate(),
            repository=InMemoryTeamRepository(),
            events=LocalTeamEventPublisher(),
            aggregator=ConcatenateResultAggregator(),
        )
    )

    result = await orchestrator.run(TeamRequest("触发组件超时"))

    assert result.status is TeamStatus.FAILED
    assert result.stop_reason is TeamStopReason.COMPONENT_ERROR
    assert "planner backend timed out" in result.error


async def test_team_timeout_includes_initial_lifecycle_events() -> None:
    """总时限应从 run 入口计算，而不是在初始事件完成后重新计时。"""
    events = LocalTeamEventPublisher()

    async def slow_start(event: TeamEvent) -> None:
        if event.event_type is TeamEventType.TEAM_STARTED:
            await asyncio.sleep(0.03)

    events.subscribe(slow_start)
    worker = _endpoint("worker", "python")
    orchestrator = _orchestrator(
        (TaskSpec("task", "不应启动", "python"),),
        (worker,),
        events=events,
    )

    result = await orchestrator.run(
        TeamRequest("初始事件超时", limits=TeamLimits(timeout_seconds=0.01))
    )

    assert result.status is TeamStatus.TIMED_OUT
    assert worker.calls == []


async def test_invalid_approval_decision_is_component_error() -> None:
    """审批门返回未知值时不得静默解释为业务拒绝。"""

    class InvalidGate:
        async def decide(self, context: AgentTaskContext):
            del context
            return "unknown"

    worker = _endpoint("worker", "write")
    orchestrator = _orchestrator(
        (TaskSpec("write", "执行写入", "write", requires_approval=True),),
        (worker,),
        approval_gate=InvalidGate(),  # type: ignore[arg-type]
    )

    result = await orchestrator.run(TeamRequest("审批异常"))

    assert result.status is TeamStatus.FAILED
    assert result.stop_reason is TeamStopReason.COMPONENT_ERROR
    assert "invalid decision" in result.error
    assert worker.calls == []


async def test_resume_verifying_continues_verifier_without_rerunning_endpoint() -> None:
    """验证阶段崩溃后应复用执行结果，不得重复触发有副作用的 Agent。"""

    class CountingVerifier:
        def __init__(self) -> None:
            self.calls = 0

        async def verify(
            self,
            context: AgentTaskContext,
            result: TaskResult,
        ) -> TaskVerification:
            del context, result
            self.calls += 1
            return TaskVerification(True, score=100, evidence=("恢复验证",))

    repository = InMemoryTeamRepository()
    task = TaskSpec("task", "有副作用的执行", "python")
    execution = TaskResult("task", "worker", True, output="已执行", attempt=1)
    await repository.create(
        TeamSnapshot(
            TeamRequest("恢复验证"),
            (
                TaskState(
                    task,
                    status=TaskStatus.VERIFYING,
                    attempt=1,
                    assigned_agent="worker",
                    result=execution,
                ),
            ),
            run_id="resume-verifying",
            status=TeamStatus.RUNNING,
        )
    )
    verifier = CountingVerifier()
    endpoint = _endpoint("worker", "python")
    orchestrator = _orchestrator(
        (task,),
        (endpoint,),
        verifier=verifier,
        repository=repository,
    )

    result = await orchestrator.resume("resume-verifying")

    assert result.status is TeamStatus.COMPLETED
    assert result.output == "已执行"
    assert endpoint.calls == []
    assert verifier.calls == 1


async def test_resume_applies_persisted_verification_without_model_call() -> None:
    """已持久化的验证结论应直接推进状态，不得重复调用验证模型。"""

    class UnexpectedVerifier:
        async def verify(
            self,
            context: AgentTaskContext,
            result: TaskResult,
        ) -> TaskVerification:
            raise AssertionError((context, result))

    repository = InMemoryTeamRepository()
    task = TaskSpec("task", "已有验证", "python")
    execution = TaskResult("task", "worker", True, output="已执行", attempt=1)
    verification = TaskVerification(True, score=100, evidence=("已保存",))
    await repository.create(
        TeamSnapshot(
            TeamRequest("应用验证"),
            (
                TaskState(
                    task,
                    status=TaskStatus.VERIFYING,
                    attempt=1,
                    assigned_agent="worker",
                    result=execution,
                    verification=verification,
                ),
            ),
            run_id="persisted-verification",
            status=TeamStatus.RUNNING,
        )
    )
    endpoint = _endpoint("worker", "python")
    orchestrator = _orchestrator(
        (task,),
        (endpoint,),
        verifier=UnexpectedVerifier(),
        repository=repository,
    )

    result = await orchestrator.resume("persisted-verification")

    assert result.status is TeamStatus.COMPLETED
    assert endpoint.calls == []


async def test_failed_verification_is_not_counted_as_completed_task() -> None:
    """执行成功但验收失败的结果只能作为审计证据，不能计入完成数量。"""

    class RejectVerifier:
        async def verify(
            self,
            context: AgentTaskContext,
            result: TaskResult,
        ) -> TaskVerification:
            del context, result
            return TaskVerification(False, feedback="验收失败", score=20)

    repository = InMemoryTeamRepository()
    task = TaskSpec("task", "产生未通过结果", "python")
    endpoint = _endpoint("worker", "python")
    orchestrator = _orchestrator(
        (task,),
        (endpoint,),
        verifier=RejectVerifier(),
        repository=repository,
    )

    result = await orchestrator.run(
        TeamRequest("拒绝结果", limits=TeamLimits(max_task_attempts=1)),
        run_id="failed-verification",
    )
    snapshot = await repository.require("failed-verification")

    assert result.status is TeamStatus.FAILED
    assert result.completed_tasks == 0
    assert result.task_results == ()
    assert snapshot.tasks[0].result is not None
    assert snapshot.tasks[0].verification is not None
    assert snapshot.tasks[0].verification.passed is False


def test_local_team_runtime_uses_dedicated_event_loop_thread() -> None:
    """同步门面应能在专用事件循环线程完整执行并安全关闭。"""
    worker = _endpoint("worker", "python")
    orchestrator = _orchestrator(
        (TaskSpec("task", "完成同步任务", "python"),),
        (worker,),
    )

    with LocalTeamRuntime(AsyncTeamRuntime(orchestrator)) as runtime:
        result = runtime.run(TeamRequest("同步运行"))
        loaded = runtime.get(result.run_id)

    assert result.status is TeamStatus.COMPLETED
    assert loaded == result


def test_local_submit_and_close_schedule_atomically(monkeypatch: pytest.MonkeyPatch) -> None:
    """同步提交与关闭竞态不得把协程投递到已经关闭的事件循环。"""
    orchestrator = _orchestrator(
        (TaskSpec("task", "竞态测试", "python"),),
        (_endpoint("worker", "python"),),
    )
    runtime = LocalTeamRuntime(AsyncTeamRuntime(orchestrator))
    original_submit = team_runtime_module.asyncio.run_coroutine_threadsafe
    submit_entered = threading.Event()
    allow_submit = threading.Event()
    close_finished = threading.Event()

    def delayed_submit(coroutine, loop):
        code = getattr(coroutine, "cr_code", None)
        if code is not None and code.co_name == "get":
            submit_entered.set()
            if not allow_submit.wait(timeout=1):
                raise TimeoutError("test did not release submission")
        return original_submit(coroutine, loop)

    monkeypatch.setattr(
        team_runtime_module.asyncio,
        "run_coroutine_threadsafe",
        delayed_submit,
    )

    def read_missing() -> BaseException | None:
        try:
            runtime.get("missing")
        except BaseException as exc:
            return exc
        return None

    def close_runtime() -> None:
        runtime.close()
        close_finished.set()

    with ThreadPoolExecutor(max_workers=2) as pool:
        read_future = pool.submit(read_missing)
        assert submit_entered.wait(timeout=1)
        close_future = pool.submit(close_runtime)
        assert close_finished.wait(timeout=0.03) is False
        allow_submit.set()
        read_error = read_future.result(timeout=2)
        close_future.result(timeout=2)

    assert not (isinstance(read_error, RuntimeError) and "Event loop is closed" in str(read_error))


async def test_loop_agent_endpoint_maps_injected_runtime_without_environment_access() -> None:
    """Loop 端点只使用注入运行时，并保留团队关联字段。"""

    class FakeLoopRuntime:
        def __init__(self) -> None:
            self.request = None
            self.run_id = None

        async def run(self, request, *, run_id=None) -> LoopResult:
            self.request = request
            self.run_id = run_id
            return LoopResult(
                run_id=run_id,
                status=LoopStatus.COMPLETED,
                output="Loop 已完成",
                cycles=1,
                total_attempts=1,
                completed_steps=1,
                records=(),
                stop_reason=StopReason.COMPLETED,
            )

    loop_runtime = FakeLoopRuntime()
    endpoint = LoopAgentEndpoint(
        AgentSpec("loop-agent", frozenset({"python"})),
        loop_runtime,
        metadata={"source": "explicit"},
    )
    context = AgentTaskContext(
        team_run_id="team-run",
        request=TeamRequest("完成团队目标", acceptance_criteria=("团队验收",)),
        task=TaskSpec("task", "完成子任务", "python"),
        agent_id="loop-agent",
        attempt=1,
    )

    result = await endpoint.execute(context)

    assert result.success is True
    assert result.output == "Loop 已完成"
    assert loop_runtime.run_id == "team-run--task--loop-agent--1"
    assert loop_runtime.request.goal == "完成子任务"
    assert loop_runtime.request.acceptance_criteria == ("团队验收",)
    assert loop_runtime.request.metadata["source"] == "explicit"
