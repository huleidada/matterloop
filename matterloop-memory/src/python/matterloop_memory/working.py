"""当前任务上下文的 Working Memory 协议与内存实现。"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from types import MappingProxyType
from typing import Protocol, runtime_checkable


@dataclass(frozen=True, slots=True)
class PlanStepSummary:
    """描述计划内单个步骤的轻量摘要。"""

    step_id: str
    description: str
    status: str

    def __post_init__(self) -> None:
        """校验步骤标识非空。"""
        if not self.step_id.strip():
            raise ValueError("step_id must not be empty")


@dataclass(frozen=True, slots=True)
class WorkingMemorySnapshot:
    """表示一次运行的当前任务上下文快照。"""

    run_id: str
    goal: str
    plan: tuple[PlanStepSummary, ...] = ()
    current_step_index: int = 0
    step_results: Mapping[str, str] = field(default_factory=dict)
    metadata: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        """校验必填字段并冻结映射字段。"""
        if not self.run_id.strip():
            raise ValueError("run_id must not be empty")
        if not self.goal.strip():
            raise ValueError("goal must not be empty")
        if self.current_step_index < 0:
            raise ValueError("current_step_index must not be negative")
        object.__setattr__(self, "step_results", MappingProxyType(dict(self.step_results)))
        object.__setattr__(self, "metadata", MappingProxyType(dict(self.metadata)))


@runtime_checkable
class WorkingMemory(Protocol):
    """Working Memory 存储扩展协议。"""

    async def save_snapshot(self, snapshot: WorkingMemorySnapshot) -> None:
        """保存或替换指定运行的上下文快照。"""
        ...

    async def load_snapshot(self, run_id: str) -> WorkingMemorySnapshot | None:
        """按运行标识读取上下文快照。"""
        ...

    async def record_step_result(
        self, run_id: str, step_id: str, result: str
    ) -> WorkingMemorySnapshot:
        """记录步骤中间执行结果并返回更新后的快照。"""
        ...

    async def clear(self, run_id: str) -> bool:
        """清理指定运行的工作记忆并返回其是否存在。"""
        ...


class InMemoryWorkingMemory:
    """并发安全的 Working Memory 内存实现。"""

    def __init__(self) -> None:
        self._snapshots: dict[str, WorkingMemorySnapshot] = {}
        self._lock = asyncio.Lock()

    async def save_snapshot(self, snapshot: WorkingMemorySnapshot) -> None:
        """保存或替换指定运行的上下文快照。

        Args:
            snapshot: 需要持久化的工作记忆快照。
        """
        async with self._lock:
            self._snapshots[snapshot.run_id] = snapshot

    async def load_snapshot(self, run_id: str) -> WorkingMemorySnapshot | None:
        """按运行标识读取上下文快照。

        Args:
            run_id: 运行标识。

        Returns:
            对应的快照；不存在时返回 None。
        """
        async with self._lock:
            return self._snapshots.get(run_id)

    async def record_step_result(
        self, run_id: str, step_id: str, result: str
    ) -> WorkingMemorySnapshot:
        """记录步骤中间执行结果并返回更新后的快照。

        Args:
            run_id: 运行标识。
            step_id: 产生结果的步骤标识。
            result: 步骤的中间执行结果文本。

        Returns:
            合并该步骤结果之后的新快照。

        Raises:
            KeyError: 指定运行不存在已保存的快照。
            ValueError: step_id 为空。
        """
        if not step_id.strip():
            raise ValueError("step_id must not be empty")
        async with self._lock:
            snapshot = self._snapshots.get(run_id)
            if snapshot is None:
                raise KeyError(f"working memory snapshot not found for run {run_id}")
            results = dict(snapshot.step_results)
            results[step_id] = result
            updated = replace(snapshot, step_results=results)
            self._snapshots[run_id] = updated
            return updated

    async def clear(self, run_id: str) -> bool:
        """清理指定运行的工作记忆。

        Args:
            run_id: 运行标识。

        Returns:
            快照存在且被删除时为 True。
        """
        async with self._lock:
            return self._snapshots.pop(run_id, None) is not None
