"""长期记忆协议和值对象。"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from types import MappingProxyType
from typing import Protocol, runtime_checkable
from uuid import uuid4


class MemoryKind(str, Enum):
    """记忆内容的稳定分类。"""

    SEMANTIC = "semantic"
    EPISODIC = "episodic"
    PROCEDURAL = "procedural"


@dataclass(frozen=True, slots=True)
class MemoryRecord:
    """表示一条可持久化的长期记忆。"""

    namespace: str
    kind: MemoryKind
    content: str
    metadata: Mapping[str, str] = field(default_factory=dict)
    record_id: str = field(default_factory=lambda: uuid4().hex)
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    expires_at: datetime | None = None

    def __post_init__(self) -> None:
        """冻结元数据并校验必填字段。"""
        if not self.namespace.strip():
            raise ValueError("namespace must not be empty")
        if not self.content.strip():
            raise ValueError("content must not be empty")
        object.__setattr__(self, "metadata", MappingProxyType(dict(self.metadata)))


@dataclass(frozen=True, slots=True)
class MemoryQuery:
    """描述一次长期记忆检索。"""

    namespace: str
    text: str | None = None
    kinds: tuple[MemoryKind, ...] = ()
    limit: int = 10
    min_score: float = 0
    filters: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        """校验查询边界并冻结过滤条件。"""
        if not self.namespace.strip():
            raise ValueError("namespace must not be empty")
        if self.limit < 1:
            raise ValueError("limit must be at least 1")
        if not 0 <= self.min_score <= 1:
            raise ValueError("min_score must be between 0 and 1")
        object.__setattr__(self, "filters", MappingProxyType(dict(self.filters)))


@dataclass(frozen=True, slots=True)
class MemoryMatch:
    """保存记忆检索结果与相关度。"""

    record: MemoryRecord
    score: float


@runtime_checkable
class MemoryStore(Protocol):
    """长期记忆存储扩展协议。"""

    async def put(self, record: MemoryRecord) -> None:
        """新增或替换一条记忆。"""
        ...

    async def get(self, record_id: str) -> MemoryRecord | None:
        """按标识读取一条记忆。"""
        ...

    async def search(self, query: MemoryQuery) -> tuple[MemoryMatch, ...]:
        """按命名空间和查询条件检索记忆。"""
        ...

    async def delete(self, record_id: str) -> bool:
        """删除一条记忆并返回其是否存在。"""
        ...

    async def clear(self, namespace: str) -> int:
        """清理命名空间并返回删除数量。"""
        ...
