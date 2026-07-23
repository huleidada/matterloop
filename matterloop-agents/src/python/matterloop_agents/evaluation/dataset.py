"""评测任务定义与可过滤的任务数据集。"""

from __future__ import annotations

from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass, field
from enum import Enum
from types import MappingProxyType


class TaskKind(str, Enum):
    """评测任务所属的用途分类。"""

    BENCHMARK = "benchmark"
    GOLDEN = "golden"
    REGRESSION = "regression"


@dataclass(frozen=True, slots=True)
class EvaluationTask:
    """描述一条可重复执行的评测任务。

    Args:
        task_id: 数据集内稳定唯一的任务标识。
        kind: 任务用途分类。
        goal: 交给 Loop 执行的目标描述。
        acceptance_criteria: 判定任务完成所需满足的条件。
        reference_output: 可选的参考输出，用于人工或模型比对。
        domain_tags: 领域标签，用于按领域筛选任务。
        expected_numeric: 可选的期望数值，供预测误差类指标使用。
        metadata: 原样传递的只读扩展数据。
    """

    task_id: str
    kind: TaskKind
    goal: str
    acceptance_criteria: tuple[str, ...] = ()
    reference_output: str | None = None
    domain_tags: tuple[str, ...] = ()
    expected_numeric: float | None = None
    metadata: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        """校验任务关键字段并冻结扩展数据。"""
        if not self.task_id.strip():
            raise ValueError("task_id must not be empty")
        if not self.goal.strip():
            raise ValueError("goal must not be empty")
        object.__setattr__(self, "metadata", MappingProxyType(dict(self.metadata)))


class EvaluationDataset:
    """按 ``task_id`` 去重的评测任务集合，支持按分类与标签过滤。

    Args:
        tasks: 初始任务序列；重复 ``task_id`` 时保留首次出现的任务。
    """

    def __init__(self, tasks: Iterable[EvaluationTask]) -> None:
        deduplicated: dict[str, EvaluationTask] = {}
        for task in tasks:
            deduplicated.setdefault(task.task_id, task)
        self._tasks: tuple[EvaluationTask, ...] = tuple(deduplicated.values())

    @property
    def tasks(self) -> tuple[EvaluationTask, ...]:
        """返回去重后的全部任务。"""
        return self._tasks

    def __len__(self) -> int:
        """返回任务数量。"""
        return len(self._tasks)

    def __iter__(self) -> Iterator[EvaluationTask]:
        """按插入顺序迭代任务。"""
        return iter(self._tasks)

    def filter(
        self,
        *,
        kinds: Iterable[TaskKind] | None = None,
        tags: Iterable[str] | None = None,
    ) -> tuple[EvaluationTask, ...]:
        """按分类与领域标签过滤任务。

        Args:
            kinds: 允许的任务分类；为 ``None`` 时不按分类过滤。
            tags: 需要命中的领域标签，任一命中即保留；为 ``None`` 时不按标签过滤。

        Returns:
            满足全部过滤条件的任务元组。
        """
        allowed_kinds = None if kinds is None else frozenset(kinds)
        wanted_tags = None if tags is None else frozenset(tags)
        selected: list[EvaluationTask] = []
        for task in self._tasks:
            if allowed_kinds is not None and task.kind not in allowed_kinds:
                continue
            if wanted_tags is not None and not wanted_tags.intersection(task.domain_tags):
                continue
            selected.append(task)
        return tuple(selected)


__all__ = ["EvaluationDataset", "EvaluationTask", "TaskKind"]
