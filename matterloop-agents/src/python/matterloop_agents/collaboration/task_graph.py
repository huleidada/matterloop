"""可恢复的多智能体任务 DAG 与严格状态转换。"""

from __future__ import annotations

from dataclasses import replace

from matterloop_agents.collaboration.errors import (
    InvalidTaskGraphError,
    InvalidTaskTransitionError,
)
from matterloop_agents.collaboration.models import (
    TaskResult,
    TaskSpec,
    TaskState,
    TaskStatus,
    TaskVerification,
    TeamSnapshot,
)

_IN_PROGRESS = frozenset({TaskStatus.WAITING_APPROVAL, TaskStatus.RUNNING, TaskStatus.VERIFYING})
_FAILED_DEPENDENCIES = frozenset({TaskStatus.FAILED, TaskStatus.BLOCKED, TaskStatus.CANCELLED})


class TaskGraph:
    """管理任务依赖、就绪计算和可审计状态转换。

    Args:
        tasks: 需要组成有向无环图的任务定义。
        states: 恢复运行时使用的已有任务状态；缺省时创建初始状态。

    Raises:
        InvalidTaskGraphError: 任务为空、标识重复、依赖缺失或存在环。
    """

    def __init__(
        self,
        tasks: tuple[TaskSpec, ...],
        *,
        states: tuple[TaskState, ...] | None = None,
    ) -> None:
        self._validate(tasks)
        self._tasks = {task.task_id: task for task in tasks}
        if states is None:
            self._states = {
                task.task_id: TaskState(
                    task,
                    status=TaskStatus.READY if not task.dependencies else TaskStatus.PENDING,
                )
                for task in tasks
            }
        else:
            state_by_id = {state.spec.task_id: state for state in states}
            if set(state_by_id) != set(self._tasks):
                raise InvalidTaskGraphError("restored states do not match task identifiers")
            if any(state.spec != self._tasks[task_id] for task_id, state in state_by_id.items()):
                raise InvalidTaskGraphError("restored task definitions do not match the graph")
            self._states = state_by_id
            self._validate_restored_dependencies()
            self._refresh_pending()

    @classmethod
    def from_snapshot(cls, snapshot: TeamSnapshot) -> TaskGraph:
        """从持久化快照恢复任务图。

        Args:
            snapshot: 包含全部任务状态的团队快照。

        Returns:
            与快照状态隔离的任务图。
        """
        return cls(
            tuple(state.spec for state in snapshot.tasks),
            states=snapshot.tasks,
        )

    def state(self, task_id: str) -> TaskState:
        """返回指定任务的当前不可变状态。

        Args:
            task_id: 目标任务标识。

        Returns:
            当前任务状态。

        Raises:
            InvalidTaskGraphError: 任务标识不存在。
        """
        try:
            return self._states[task_id]
        except KeyError as exc:
            raise InvalidTaskGraphError(f"unknown task identifier: {task_id}") from exc

    def states(self) -> tuple[TaskState, ...]:
        """按稳定任务标识返回全部状态快照。"""
        return tuple(self._states[task_id] for task_id in sorted(self._states))

    def ready_tasks(self) -> tuple[TaskSpec, ...]:
        """按优先级和标识返回当前可以分配的任务。"""
        self._refresh_pending()
        ready = [state.spec for state in self._states.values() if state.status is TaskStatus.READY]
        return tuple(sorted(ready, key=lambda task: (-task.priority, task.task_id)))

    def mark_waiting_approval(self, task_id: str) -> TaskState:
        """把就绪任务转换为等待审批。"""
        return self._transition(task_id, TaskStatus.WAITING_APPROVAL, {TaskStatus.READY})

    def resume_approval(self, task_id: str) -> TaskState:
        """记录审批已经通过，并把任务恢复为可调度状态。"""
        current = self._require_status(task_id, {TaskStatus.WAITING_APPROVAL})
        updated = replace(current, status=TaskStatus.READY, approval_granted=True)
        self._states[task_id] = updated
        return updated

    def start(self, task_id: str, agent_id: str) -> TaskState:
        """原子记录任务分配并增加尝试次数。"""
        current = self._require_status(task_id, {TaskStatus.READY})
        updated = replace(
            current,
            status=TaskStatus.RUNNING,
            attempt=current.attempt + 1,
            assigned_agent=agent_id,
            result=None,
            verification=None,
            error="",
        )
        self._states[task_id] = updated
        return updated

    def begin_verification(
        self,
        task_id: str,
        result: TaskResult | None = None,
    ) -> TaskState:
        """把已执行任务转换为独立验证阶段并保存待验证结果。"""
        current = self._require_status(task_id, {TaskStatus.RUNNING})
        if result is None:
            raise InvalidTaskTransitionError("verification requires an execution result")
        if result.task_id != task_id or not result.success:
            raise InvalidTaskTransitionError(
                "verification requires the task's own successful result"
            )
        if result.agent_id != current.assigned_agent or result.attempt != current.attempt:
            raise InvalidTaskTransitionError(
                "verification result must match the assigned agent and attempt"
            )
        updated = replace(
            current,
            status=TaskStatus.VERIFYING,
            result=result,
            verification=None,
        )
        self._states[task_id] = updated
        return updated

    def record_verification(
        self,
        task_id: str,
        verification: TaskVerification,
    ) -> TaskState:
        """在验证阶段保存结构化验证证据。"""
        current = self._require_status(task_id, {TaskStatus.VERIFYING})
        if current.result is None:
            raise InvalidTaskTransitionError("verification requires a stored execution result")
        updated = replace(current, verification=verification)
        self._states[task_id] = updated
        return updated

    def succeed(self, task_id: str, result: TaskResult) -> TaskState:
        """保存通过验证的结果并解锁下游任务。"""
        current = self._require_status(task_id, {TaskStatus.VERIFYING})
        if result.task_id != task_id or not result.success:
            raise InvalidTaskTransitionError("succeeded task requires its own successful result")
        if result.agent_id != current.assigned_agent or result.attempt != current.attempt:
            raise InvalidTaskTransitionError(
                "succeeded result must match the assigned agent and attempt"
            )
        if current.verification is None or not current.verification.passed:
            raise InvalidTaskTransitionError("succeeded task requires a passed verification")
        updated = replace(
            current,
            status=TaskStatus.SUCCEEDED,
            result=result,
            error="",
        )
        self._states[task_id] = updated
        self._refresh_pending()
        return updated

    def retry(
        self,
        task_id: str,
        error: str,
        *,
        result: TaskResult | None = None,
        verification: TaskVerification | None = None,
    ) -> TaskState:
        """保留尝试计数并把失败执行恢复为就绪状态。"""
        current = self._require_status(task_id, _IN_PROGRESS | {TaskStatus.READY})
        updated = replace(
            current,
            status=TaskStatus.READY,
            assigned_agent=None,
            result=result,
            verification=verification,
            error=error,
        )
        self._states[task_id] = updated
        return updated

    def fail(
        self,
        task_id: str,
        error: str,
        *,
        result: TaskResult | None = None,
        verification: TaskVerification | None = None,
    ) -> TaskState:
        """终止任务并阻断依赖它的全部下游节点。"""
        current = self._require_status(
            task_id,
            _IN_PROGRESS | {TaskStatus.READY, TaskStatus.PENDING},
        )
        updated = replace(
            current,
            status=TaskStatus.FAILED,
            result=result,
            verification=verification,
            error=error,
        )
        self._states[task_id] = updated
        self._refresh_pending()
        return updated

    def cancel_all(self) -> None:
        """取消所有尚未成功或失败的任务。"""
        for task_id, state in tuple(self._states.items()):
            if not state.status.is_terminal:
                self._states[task_id] = replace(state, status=TaskStatus.CANCELLED)
        self._refresh_pending()

    def recover_inflight(self) -> None:
        """恢复未完成执行；验证中节点保留结果并由控制器精确继续。"""
        for task_id, state in tuple(self._states.items()):
            if state.status in {TaskStatus.WAITING_APPROVAL, TaskStatus.RUNNING}:
                self._states[task_id] = replace(
                    state,
                    status=TaskStatus.READY,
                    assigned_agent=None,
                    result=None,
                    verification=None,
                    error="recovered from interrupted execution",
                )
        self._refresh_pending()

    def _validate_restored_dependencies(self) -> None:
        executable = {
            TaskStatus.READY,
            TaskStatus.WAITING_APPROVAL,
            TaskStatus.RUNNING,
            TaskStatus.VERIFYING,
            TaskStatus.SUCCEEDED,
        }
        for task_id, state in self._states.items():
            if state.status not in executable:
                continue
            unmet = tuple(
                dependency
                for dependency in state.spec.dependencies
                if self._states[dependency].status is not TaskStatus.SUCCEEDED
            )
            if unmet:
                values = ", ".join(unmet)
                raise InvalidTaskGraphError(
                    f"restored task {task_id!r} is executable before dependencies: {values}"
                )

    @property
    def all_succeeded(self) -> bool:
        """判断全部任务是否已经成功。"""
        return all(state.status is TaskStatus.SUCCEEDED for state in self._states.values())

    @property
    def is_terminal(self) -> bool:
        """判断全部任务是否都已进入终态。"""
        return all(state.status.is_terminal for state in self._states.values())

    @property
    def has_failures(self) -> bool:
        """判断任务图是否包含失败、阻断或取消节点。"""
        return any(state.status in _FAILED_DEPENDENCIES for state in self._states.values())

    def successful_results(self) -> tuple[TaskResult, ...]:
        """按任务标识返回所有成功结果。"""
        return tuple(
            state.result
            for state in self.states()
            if state.status is TaskStatus.SUCCEEDED and state.result is not None
        )

    def dependency_results(self, task_id: str) -> tuple[TaskResult, ...]:
        """返回指定任务全部已成功依赖的结果。"""
        task = self.state(task_id).spec
        return tuple(
            result
            for dependency in task.dependencies
            if (result := self.state(dependency).result) is not None
        )

    def _transition(
        self,
        task_id: str,
        target: TaskStatus,
        allowed: set[TaskStatus] | frozenset[TaskStatus],
    ) -> TaskState:
        current = self._require_status(task_id, allowed)
        updated = replace(current, status=target)
        self._states[task_id] = updated
        return updated

    def _require_status(
        self,
        task_id: str,
        allowed: set[TaskStatus] | frozenset[TaskStatus],
    ) -> TaskState:
        state = self.state(task_id)
        if state.status not in allowed:
            values = ", ".join(sorted(item.value for item in allowed))
            raise InvalidTaskTransitionError(
                f"task {task_id!r} cannot leave {state.status.value}; expected one of {values}"
            )
        return state

    def _refresh_pending(self) -> None:
        changed = True
        while changed:
            changed = False
            for task_id, state in tuple(self._states.items()):
                if state.status is not TaskStatus.PENDING:
                    continue
                dependency_statuses = {
                    self._states[dependency].status for dependency in state.spec.dependencies
                }
                if dependency_statuses & _FAILED_DEPENDENCIES:
                    self._states[task_id] = replace(
                        state,
                        status=TaskStatus.BLOCKED,
                        error="dependency did not complete successfully",
                    )
                    changed = True
                elif all(status is TaskStatus.SUCCEEDED for status in dependency_statuses):
                    self._states[task_id] = replace(state, status=TaskStatus.READY)
                    changed = True

    @staticmethod
    def _validate(tasks: tuple[TaskSpec, ...]) -> None:
        if not tasks:
            raise InvalidTaskGraphError("task graph must contain at least one task")
        task_ids = [task.task_id for task in tasks]
        if len(task_ids) != len(set(task_ids)):
            raise InvalidTaskGraphError("task graph contains duplicate task identifiers")
        known = set(task_ids)
        for task in tasks:
            missing = set(task.dependencies) - known
            if missing:
                values = ", ".join(sorted(missing))
                raise InvalidTaskGraphError(
                    f"task {task.task_id!r} references missing dependencies: {values}"
                )

        remaining = {task.task_id: set(task.dependencies) for task in tasks}
        ready = [task_id for task_id, dependencies in remaining.items() if not dependencies]
        visited = 0
        while ready:
            current = ready.pop()
            visited += 1
            for task_id, dependencies in remaining.items():
                if current not in dependencies:
                    continue
                dependencies.remove(current)
                if not dependencies:
                    ready.append(task_id)
        if visited != len(tasks):
            raise InvalidTaskGraphError("task graph contains a dependency cycle")


__all__ = ["TaskGraph"]
