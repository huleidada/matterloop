"""多 Agent 任务 DAG 校验和严格状态转换测试。"""

from __future__ import annotations

from dataclasses import replace

import pytest
from matterloop_agents.collaboration import (
    InvalidTaskGraphError,
    InvalidTaskTransitionError,
    TaskGraph,
    TaskResult,
    TaskSpec,
    TaskState,
    TaskStatus,
    TaskVerification,
    TeamRequest,
    TeamSnapshot,
)


def _result(task_id: str, *, attempt: int = 1) -> TaskResult:
    """构造与图状态匹配的成功结果。"""
    return TaskResult(task_id, "worker", True, output=task_id, attempt=attempt)


def test_task_graph_unlocks_dependencies_in_priority_order() -> None:
    """图只应暴露依赖已成功的节点，并按优先级稳定排序。"""
    graph = TaskGraph(
        (
            TaskSpec("root-b", "根任务 B", "python", priority=1),
            TaskSpec("root-a", "根任务 A", "python", priority=2),
            TaskSpec(
                "merge",
                "汇总",
                "review",
                dependencies=("root-a", "root-b"),
            ),
        )
    )

    assert tuple(task.task_id for task in graph.ready_tasks()) == ("root-a", "root-b")
    graph.start("root-a", "worker")
    root_a_result = _result("root-a")
    graph.begin_verification("root-a", root_a_result)
    graph.record_verification("root-a", TaskVerification(True))
    graph.succeed("root-a", root_a_result)
    assert tuple(task.task_id for task in graph.ready_tasks()) == ("root-b",)
    graph.start("root-b", "worker")
    root_b_result = _result("root-b")
    graph.begin_verification("root-b", root_b_result)
    graph.record_verification("root-b", TaskVerification(True))
    graph.succeed("root-b", root_b_result)

    assert tuple(task.task_id for task in graph.ready_tasks()) == ("merge",)
    assert tuple(item.task_id for item in graph.dependency_results("merge")) == (
        "root-a",
        "root-b",
    )


@pytest.mark.parametrize(
    "tasks, message",
    [
        ((), "at least one"),
        (
            (
                TaskSpec("same", "任务一", "python"),
                TaskSpec("same", "任务二", "python"),
            ),
            "duplicate",
        ),
        (
            (TaskSpec("task", "任务", "python", dependencies=("missing",)),),
            "missing",
        ),
        (
            (
                TaskSpec("a", "任务 A", "python", dependencies=("b",)),
                TaskSpec("b", "任务 B", "python", dependencies=("a",)),
            ),
            "cycle",
        ),
    ],
)
def test_task_graph_rejects_invalid_dag(
    tasks: tuple[TaskSpec, ...],
    message: str,
) -> None:
    """空图、重复标识、缺失依赖和环都必须在执行前失败。"""
    with pytest.raises(InvalidTaskGraphError, match=message):
        TaskGraph(tasks)


def test_task_graph_rejects_out_of_order_state_transitions() -> None:
    """执行、验证和成功转换不得绕过前置状态。"""
    graph = TaskGraph((TaskSpec("task", "执行任务", "python"),))

    with pytest.raises(InvalidTaskTransitionError):
        graph.begin_verification("task", _result("task"))
    graph.start("task", "worker")
    with pytest.raises(InvalidTaskTransitionError):
        graph.succeed("task", _result("task"))
    graph.begin_verification("task", _result("task"))
    with pytest.raises(InvalidTaskTransitionError, match="successful result"):
        graph.succeed("task", TaskResult("task", "worker", False, error="失败"))


def test_failed_task_blocks_all_transitive_dependants() -> None:
    """一个任务终止失败后，全部传递下游节点都应成为阻断终态。"""
    graph = TaskGraph(
        (
            TaskSpec("root", "根任务", "python"),
            TaskSpec("middle", "中间任务", "python", dependencies=("root",)),
            TaskSpec("leaf", "叶子任务", "python", dependencies=("middle",)),
        )
    )
    graph.start("root", "worker")
    graph.fail("root", "执行失败")

    assert tuple(state.status for state in graph.states()) == (
        TaskStatus.BLOCKED,
        TaskStatus.BLOCKED,
        TaskStatus.FAILED,
    )
    assert graph.is_terminal is True
    assert graph.has_failures is True


def test_recovery_blocks_non_replayable_inflight_task() -> None:
    """默认任务的执行状态不明确时不得重复调用 Agent。"""
    task = TaskSpec("task", "执行任务", "python")
    graph = TaskGraph((task,))
    graph.start("task", "worker")
    snapshot = TeamSnapshot(TeamRequest("恢复任务"), graph.states(), run_id="recover-run")
    restored = TaskGraph.from_snapshot(snapshot)

    blocked = restored.recover_inflight()
    recovered = restored.state("task")

    assert blocked == ("task",)
    assert recovered.status is TaskStatus.BLOCKED
    assert recovered.attempt == 1
    assert recovered.assigned_agent == "worker"


def test_recovery_replays_only_explicitly_safe_task() -> None:
    """宿主显式声明可重放后，恢复才允许开始下一次尝试。"""
    task = TaskSpec("task", "只读任务", "python", replay_safe=True)
    graph = TaskGraph((task,))
    graph.start("task", "worker")

    assert graph.recover_inflight() == ()
    recovered = graph.state("task")
    restarted = graph.start("task", "worker-2")

    assert recovered.status is TaskStatus.READY
    assert recovered.attempt == 1
    assert restarted.attempt == 2


def test_snapshot_restore_rejects_changed_task_definition() -> None:
    """恢复状态中的任务定义不得与当前图定义悄然分叉。"""
    original = TaskSpec("task", "原任务", "python")
    changed = replace(original, description="被修改的任务")
    snapshot = TeamSnapshot(
        TeamRequest("恢复任务"),
        TaskGraph((original,)).states(),
        run_id="recover-run",
    )

    with pytest.raises(InvalidTaskGraphError, match="definitions"):
        TaskGraph((changed,), states=snapshot.tasks)


def test_restore_rejects_ready_task_with_unmet_dependency() -> None:
    """伪造快照不得让下游 READY 状态绕过尚未成功的依赖。"""
    root = TaskSpec("root", "根任务", "python")
    child = TaskSpec("child", "下游任务", "python", dependencies=("root",))
    states = (
        TaskState(root, status=TaskStatus.READY),
        TaskState(child, status=TaskStatus.READY),
    )

    with pytest.raises(InvalidTaskGraphError, match="before dependencies"):
        TaskGraph((root, child), states=states)
