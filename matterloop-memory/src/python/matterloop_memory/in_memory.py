"""长期记忆与 Loop 检查点的内存实现。"""

from __future__ import annotations

import asyncio
import re
from datetime import datetime, timezone

from matterloop_core import CheckpointConflictError, LoopContext

from matterloop_memory.base import MemoryMatch, MemoryQuery, MemoryRecord


class NullMemoryStore:
    """显式禁用长期记忆时使用的空实现。"""

    async def put(self, record: MemoryRecord) -> None:
        """忽略写入。"""
        del record

    async def get(self, record_id: str) -> MemoryRecord | None:
        """始终返回不存在。"""
        del record_id
        return None

    async def search(self, query: MemoryQuery) -> tuple[MemoryMatch, ...]:
        """始终返回空结果。"""
        del query
        return ()

    async def delete(self, record_id: str) -> bool:
        """始终返回未删除。"""
        del record_id
        return False

    async def clear(self, namespace: str) -> int:
        """始终返回零。"""
        del namespace
        return 0


class InMemoryMemoryStore:
    """提供确定性词项匹配的并发安全内存记忆存储。"""

    def __init__(self) -> None:
        self._records: dict[str, MemoryRecord] = {}
        self._lock = asyncio.Lock()

    async def put(self, record: MemoryRecord) -> None:
        """新增或替换一条记忆。"""
        async with self._lock:
            self._records[record.record_id] = record

    async def get(self, record_id: str) -> MemoryRecord | None:
        """按标识读取未过期记忆。"""
        async with self._lock:
            record = self._records.get(record_id)
            return record if record is not None and not _expired(record) else None

    async def search(self, query: MemoryQuery) -> tuple[MemoryMatch, ...]:
        """按类型、元数据和简单词项相关度检索记忆。"""
        async with self._lock:
            matches = [
                MemoryMatch(record, _score(record.content, query.text))
                for record in self._records.values()
                if _matches(record, query)
            ]
        matches = [match for match in matches if match.score >= query.min_score]
        matches.sort(
            key=lambda match: (-match.score, match.record.created_at, match.record.record_id)
        )
        return tuple(matches[: query.limit])

    async def delete(self, record_id: str) -> bool:
        """删除一条记忆并返回其是否存在。"""
        async with self._lock:
            return self._records.pop(record_id, None) is not None

    async def clear(self, namespace: str) -> int:
        """删除命名空间内的全部记忆。"""
        async with self._lock:
            identifiers = [
                record_id
                for record_id, record in self._records.items()
                if record.namespace == namespace
            ]
            for record_id in identifiers:
                del self._records[record_id]
            return len(identifiers)


class InMemoryCheckpointStore:
    """用于本地 Runtime 的并发安全检查点存储。"""

    def __init__(self) -> None:
        self._contexts: dict[str, LoopContext] = {}
        self._lock = asyncio.Lock()

    async def save(
        self,
        context: LoopContext,
        *,
        expected_revision: int | None = None,
    ) -> int:
        """使用原子 revision 比较并保存隔离后的 Loop 上下文。

        Args:
            context: 需要持久化的运行上下文。
            expected_revision: 调用方读取到的 revision；省略时使用上下文值。

        Returns:
            成功提交后的新 revision。

        Raises:
            CheckpointConflictError: 存储中的 revision 已被其他调用推进。
        """
        expected = context.revision if expected_revision is None else expected_revision
        if expected < 0:
            raise ValueError("expected_revision must not be negative")
        async with self._lock:
            current = self._contexts.get(context.run_id)
            current_revision = current.revision if current is not None else 0
            if current_revision != expected:
                raise CheckpointConflictError(
                    f"checkpoint revision conflict for {context.run_id}: "
                    f"expected {expected}, found {current_revision}"
                )
            revision = expected + 1
            snapshot = context.snapshot()
            snapshot.revision = revision
            self._contexts[context.run_id] = snapshot
            return revision

    async def load(self, run_id: str) -> LoopContext | None:
        """读取指定运行的隔离检查点。"""
        async with self._lock:
            context = self._contexts.get(run_id)
            return context.snapshot() if context is not None else None

    async def delete(self, run_id: str) -> bool:
        """删除指定运行检查点。"""
        async with self._lock:
            return self._contexts.pop(run_id, None) is not None

    async def list_run_ids(self) -> tuple[str, ...]:
        """返回稳定且已排序的运行标识。"""
        async with self._lock:
            return tuple(sorted(self._contexts))


def _expired(record: MemoryRecord) -> bool:
    return record.expires_at is not None and record.expires_at <= datetime.now(timezone.utc)


def _matches(record: MemoryRecord, query: MemoryQuery) -> bool:
    if record.namespace != query.namespace or _expired(record):
        return False
    if query.kinds and record.kind not in query.kinds:
        return False
    return all(record.metadata.get(key) == value for key, value in query.filters.items())


def _score(content: str, text: str | None) -> float:
    if text is None or not text.strip():
        return 1.0
    query_terms = set(re.findall(r"\w+", text.casefold()))
    if not query_terms:
        return 1.0
    content_terms = set(re.findall(r"\w+", content.casefold()))
    return len(query_terms & content_terms) / len(query_terms)
