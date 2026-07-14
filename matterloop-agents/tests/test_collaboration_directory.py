"""Agent 目录的线程安全、容量、选择和热替换测试。"""

from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

import pytest
from matterloop_agents.collaboration.directory import AgentDirectory
from matterloop_agents.collaboration.errors import (
    AgentAlreadyRegisteredError,
    AgentCapacityError,
    AgentNotFoundError,
    NoCapableAgentError,
)
from matterloop_agents.collaboration.models import (
    AgentSpec,
    AgentTaskContext,
    TaskResult,
    TaskSpec,
)
from matterloop_agents.collaboration.scheduler import LeastBusyScheduler


@dataclass(slots=True)
class _Endpoint:
    spec: AgentSpec
    name: str

    async def execute(self, context: AgentTaskContext) -> TaskResult:
        """测试端点不执行实际任务。"""
        raise AssertionError(context)


def _endpoint(
    agent_id: str,
    *,
    name: str | None = None,
    capabilities: frozenset[str] = frozenset({"python"}),
    max_concurrency: int = 1,
) -> _Endpoint:
    return _Endpoint(
        AgentSpec(
            agent_id=agent_id,
            capabilities=capabilities,
            max_concurrency=max_concurrency,
        ),
        name or agent_id,
    )


def _task(
    description: str,
    *,
    capability: str = "python",
    task_id: str = "task-1",
) -> TaskSpec:
    return TaskSpec(
        task_id=task_id,
        description=description,
        capability=capability,
    )


def test_directory_register_replace_unregister_and_stable_candidates() -> None:
    """目录的同步变更必须原子执行并稳定排序。"""
    directory = AgentDirectory()
    first = _endpoint("b-agent")
    second = _endpoint("a-agent")
    directory.register(first)
    directory.register(second)

    assert tuple(spec.agent_id for spec in directory.candidates()) == (
        "a-agent",
        "b-agent",
    )
    with pytest.raises(AgentAlreadyRegisteredError):
        directory.register(_endpoint("a-agent"))

    replacement = _endpoint("a-agent", name="replacement", max_concurrency=2)
    directory.replace("a-agent", replacement)
    assert directory.candidates()[0].max_concurrency == 2

    with pytest.raises(ValueError, match="agent_id"):
        directory.replace("a-agent", _endpoint("different"))
    directory.unregister("b-agent")
    assert tuple(spec.agent_id for spec in directory.candidates()) == ("a-agent",)
    with pytest.raises(AgentNotFoundError):
        directory.unregister("missing")


async def test_scheduler_matches_capability_load_and_stable_agent_id() -> None:
    """调度器必须先匹配能力，再使用负载与稳定标识打破平局。"""
    scheduler = LeastBusyScheduler()
    task = _task("实现功能")
    candidates = (
        _endpoint("b-agent").spec,
        _endpoint("a-agent").spec,
        _endpoint("docs-agent", capabilities=frozenset({"writing"})).spec,
    )

    first = await scheduler.select(
        task,
        candidates,
        {"a-agent": 0, "b-agent": 0, "docs-agent": 0},
    )
    least_busy = await scheduler.select(
        task,
        candidates,
        {"a-agent": 1, "b-agent": 0, "docs-agent": 0},
    )

    assert first == "a-agent"
    assert least_busy == "b-agent"
    with pytest.raises(NoCapableAgentError):
        await scheduler.select(
            _task("数据库任务", capability="postgres", task_id="database-task"),
            candidates,
            {},
        )
    with pytest.raises(ValueError, match="negative"):
        await scheduler.select(task, candidates, {"a-agent": -1})


async def test_directory_balances_capacity_and_rejects_when_all_busy() -> None:
    """嵌套租约应按活跃数均衡，并严格遵守每个 Agent 的容量。"""
    directory = AgentDirectory()
    directory.register(_endpoint("a-agent"))
    directory.register(_endpoint("b-agent"))
    scheduler = LeastBusyScheduler()
    task = _task("并发任务")

    async with (
        directory.acquire(task, scheduler) as first,
        directory.acquire(task, scheduler) as second,
    ):
        assert first.spec.agent_id == "a-agent"
        assert second.spec.agent_id == "b-agent"
        with pytest.raises(AgentCapacityError):
            async with directory.acquire(task, scheduler):
                raise AssertionError("不应取得超过容量的租约")

    async with directory.acquire(task, scheduler) as available_again:
        assert available_again.spec.agent_id == "a-agent"


async def test_inflight_lease_keeps_old_endpoint_across_atomic_replacement() -> None:
    """替换后旧租约继续旧调用，新租约使用新端点且共享容量计数。"""
    directory = AgentDirectory()
    old = _endpoint("worker", name="old", max_concurrency=2)
    new = _endpoint("worker", name="new", max_concurrency=2)
    directory.register(old)
    scheduler = LeastBusyScheduler()
    task = _task("热替换任务")

    async with directory.acquire(task, scheduler) as old_lease:
        directory.replace("worker", new)
        async with directory.acquire(task, scheduler) as new_lease:
            assert old_lease.endpoint is old
            assert new_lease.endpoint is new
            with pytest.raises(AgentCapacityError):
                async with directory.acquire(task, scheduler):
                    raise AssertionError("替换不得重置活跃容量")
        assert old_lease.endpoint is old

    async with directory.acquire(task, scheduler) as after_drain:
        assert after_drain.endpoint is new


async def test_replacement_with_lower_limit_waits_for_old_lease_to_drain() -> None:
    """降低并发上限后，在旧租约释放前不得向新端点分派任务。"""
    directory = AgentDirectory()
    old = _endpoint("worker", name="old", max_concurrency=2)
    new = _endpoint("worker", name="new", max_concurrency=1)
    directory.register(old)
    scheduler = LeastBusyScheduler()
    task = _task("降低容量")

    async with directory.acquire(task, scheduler) as old_lease:
        directory.replace("worker", new)
        assert old_lease.endpoint is old
        with pytest.raises(AgentCapacityError):
            async with directory.acquire(task, scheduler):
                raise AssertionError("旧租约仍占用新并发上限")

    async with directory.acquire(task, scheduler) as new_lease:
        assert new_lease.endpoint is new


async def test_unregister_then_reregister_preserves_active_capacity() -> None:
    """活跃租约期间注销再注册同 ID 不得通过新槽位绕过并发上限。"""
    directory = AgentDirectory()
    old = _endpoint("worker", name="old", max_concurrency=1)
    new = _endpoint("worker", name="new", max_concurrency=1)
    directory.register(old)
    scheduler = LeastBusyScheduler()
    task = TaskSpec("task", "重新注册", "python")

    async with directory.acquire(task, scheduler) as old_lease:
        directory.unregister("worker")
        directory.register(new)
        assert old_lease.endpoint is old
        with pytest.raises(AgentCapacityError):
            async with directory.acquire(task, scheduler):
                raise AssertionError("重新注册不得重置活跃容量")

    async with directory.acquire(task, scheduler) as new_lease:
        assert new_lease.endpoint is new


async def test_directory_rejects_unknown_policy_selection() -> None:
    """自定义策略不得返回候选快照之外的 Agent。"""

    class UnknownPolicy:
        async def select(self, task, candidates, active_counts) -> str:
            del task, candidates, active_counts
            return "unknown"

    directory = AgentDirectory()
    directory.register(_endpoint("worker"))

    with pytest.raises(AgentNotFoundError, match="unknown"):
        async with directory.acquire(_task("任务"), UnknownPolicy()):
            raise AssertionError("未知 Agent 不得取得租约")


def test_directory_registration_is_thread_safe() -> None:
    """来自多个线程的独立注册不得丢失或破坏排序。"""
    directory = AgentDirectory()
    endpoints = tuple(_endpoint(f"agent-{index:03d}") for index in range(64))

    with ThreadPoolExecutor(max_workers=8) as pool:
        tuple(pool.map(directory.register, reversed(endpoints)))

    assert tuple(spec.agent_id for spec in directory.candidates()) == tuple(
        endpoint.spec.agent_id for endpoint in endpoints
    )


async def test_concurrent_acquire_never_exceeds_atomic_capacity() -> None:
    """并发选择产生竞态时，目录必须重新选择而不是超发租约。"""

    ready_count = 0
    both_selected = asyncio.Event()

    class RacingPolicy(LeastBusyScheduler):
        async def select(self, task, candidates, active_counts) -> str:
            nonlocal ready_count
            selected = await super().select(task, candidates, active_counts)
            ready_count += 1
            if ready_count == 2:
                both_selected.set()
            await both_selected.wait()
            return selected

    directory = AgentDirectory()
    directory.register(_endpoint("worker", max_concurrency=1))
    task = _task("竞态任务")

    async def try_acquire() -> bool:
        try:
            async with directory.acquire(task, RacingPolicy()):
                await asyncio.sleep(0)
                return True
        except AgentCapacityError:
            return False

    results = await asyncio.gather(try_acquire(), try_acquire())

    assert sorted(results) == [False, True]
