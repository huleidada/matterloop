"""团队快照的并发安全内存仓储。"""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import datetime, timezone

from matterloop_agents.collaboration.errors import (
    TeamRunAlreadyExistsError,
    TeamRunNotFoundError,
    TeamStateConflictError,
)
from matterloop_agents.collaboration.models import TeamSnapshot, TeamStatus


class InMemoryTeamRepository:
    """以隔离快照和乐观版本控制保存团队运行。

    该实现适用于单进程开发和测试。所有读写都受同一把异步锁保护；写入和读取时都会
    重建快照及任务元组，避免调用方持有仓储内部容器引用。
    """

    def __init__(self) -> None:
        self._snapshots: dict[str, TeamSnapshot] = {}
        self._lease_owners: dict[str, str] = {}
        self._lock = asyncio.Lock()

    async def create(self, snapshot: TeamSnapshot) -> None:
        """创建 version 为零的新团队快照。

        Args:
            snapshot: 尚未持久化的初始快照。

        Raises:
            ValueError: 初始版本不是零。
            TeamRunAlreadyExistsError: 同一运行标识已经存在。
        """
        if snapshot.version != 0:
            raise ValueError("initial team snapshot version must be 0")
        async with self._lock:
            if snapshot.run_id in self._snapshots:
                raise TeamRunAlreadyExistsError(f"team run already exists: {snapshot.run_id}")
            self._snapshots[snapshot.run_id] = self._isolate(snapshot)

    async def load(self, run_id: str) -> TeamSnapshot | None:
        """按运行标识读取隔离快照。

        Args:
            run_id: 团队运行标识。

        Returns:
            隔离快照；不存在时返回 ``None``。
        """
        if not run_id.strip():
            raise ValueError("run_id must not be empty")
        async with self._lock:
            snapshot = self._snapshots.get(run_id)
            return None if snapshot is None else self._isolate(snapshot)

    async def require(self, run_id: str) -> TeamSnapshot:
        """读取团队快照，不存在时抛出明确异常。

        Args:
            run_id: 团队运行标识。

        Returns:
            已保存的隔离快照。

        Raises:
            TeamRunNotFoundError: 运行标识不存在。
        """
        snapshot = await self.load(run_id)
        if snapshot is None:
            raise TeamRunNotFoundError(f"team run not found: {run_id}")
        return snapshot

    async def save(
        self,
        snapshot: TeamSnapshot,
        expected_version: int,
    ) -> TeamSnapshot:
        """通过比较并交换保存快照并递增版本。

        Args:
            snapshot: 基于调用方已观察版本形成的新完整快照。
            expected_version: 调用方最后观察到的版本。

        Returns:
            仓储实际保存的、version 已递增的隔离快照。

        Raises:
            ValueError: 期望版本为负数，或快照版本与期望版本不一致。
            TeamRunNotFoundError: 团队运行不存在。
            TeamStateConflictError: 仓储当前版本与期望版本不同。
        """
        if expected_version < 0:
            raise ValueError("expected_version must not be negative")
        if snapshot.version != expected_version:
            raise ValueError("snapshot version must match expected_version")
        async with self._lock:
            current = self._snapshots.get(snapshot.run_id)
            if current is None:
                raise TeamRunNotFoundError(f"team run not found: {snapshot.run_id}")
            if current.version != expected_version:
                raise TeamStateConflictError(
                    "team snapshot version conflict: "
                    f"run_id={snapshot.run_id}, expected={expected_version}, "
                    f"actual={current.version}"
                )
            saved = replace(
                snapshot,
                version=expected_version + 1,
                created_at=current.created_at,
                updated_at=datetime.now(timezone.utc),
            )
            isolated = self._isolate(saved)
            self._snapshots[snapshot.run_id] = isolated
            return self._isolate(isolated)

    async def list(
        self,
        *,
        status: TeamStatus | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> tuple[TeamSnapshot, ...]:
        """按创建时间和运行标识稳定列出隔离快照。

        Args:
            status: 可选状态过滤条件。
            limit: 最大返回数量。
            offset: 跳过的匹配快照数量。

        Returns:
            创建时间升序、同时间按运行标识排序的快照。
        """
        if limit < 1:
            raise ValueError("limit must be at least 1")
        if offset < 0:
            raise ValueError("offset must not be negative")
        async with self._lock:
            snapshots = (
                snapshot
                for snapshot in self._snapshots.values()
                if status is None or snapshot.status is status
            )
            ordered = sorted(
                snapshots,
                key=lambda item: (item.created_at, item.run_id),
            )
            return tuple(self._isolate(snapshot) for snapshot in ordered[offset : offset + limit])

    async def acquire_lease(self, run_id: str, owner_id: str) -> bool:
        """原子取得单进程运行租约。

        Args:
            run_id: 必须已经存在的团队运行标识。
            owner_id: 当前控制器实例标识。

        Returns:
            仅在租约空闲并由本次调用取得时返回 ``True``。

        Raises:
            TeamRunNotFoundError: 团队运行不存在。
            ValueError: 所有者标识为空。
        """
        if not owner_id.strip():
            raise ValueError("owner_id must not be empty")
        async with self._lock:
            if run_id not in self._snapshots:
                raise TeamRunNotFoundError(f"team run not found: {run_id}")
            current = self._lease_owners.get(run_id)
            if current is not None:
                return False
            self._lease_owners[run_id] = owner_id
            return True

    async def release_lease(self, run_id: str, owner_id: str) -> None:
        """只释放当前所有者持有的运行租约；其他情况保持幂等。

        Args:
            run_id: 团队运行标识。
            owner_id: 当前控制器实例标识。
        """
        async with self._lock:
            if self._lease_owners.get(run_id) == owner_id:
                self._lease_owners.pop(run_id, None)

    @staticmethod
    def _isolate(snapshot: TeamSnapshot) -> TeamSnapshot:
        # TeamSnapshot 与 TaskState 都是冻结值对象，只需重建顶层快照和任务元组。
        return replace(
            snapshot,
            tasks=tuple(task for task in snapshot.tasks),
        )


__all__ = ["InMemoryTeamRepository"]
