"""知识库、向量检索与知识图谱的 Semantic Memory 实现。"""

from __future__ import annotations

import asyncio
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Protocol, runtime_checkable
from uuid import uuid4


@dataclass(frozen=True, slots=True)
class KnowledgeDocument:
    """表示知识库中的一篇文档。"""

    title: str
    content: str
    source: str = "docs"
    tags: tuple[str, ...] = ()
    metadata: Mapping[str, str] = field(default_factory=dict)
    document_id: str = field(default_factory=lambda: uuid4().hex)

    def __post_init__(self) -> None:
        """校验必填字段并冻结元数据。"""
        if not self.title.strip():
            raise ValueError("title must not be empty")
        if not self.content.strip():
            raise ValueError("content must not be empty")
        if not self.source.strip():
            raise ValueError("source must not be empty")
        object.__setattr__(self, "metadata", MappingProxyType(dict(self.metadata)))


@dataclass(frozen=True, slots=True)
class SemanticMatch:
    """保存知识检索结果与相似度评分。"""

    document: KnowledgeDocument
    score: float


@runtime_checkable
class Embedder(Protocol):
    """文本向量化协议，由宿主注入具体模型实现。"""

    async def embed(self, texts: Sequence[str]) -> tuple[tuple[float, ...], ...]:
        """将一批文本转换为等长的向量序列。"""
        ...


@runtime_checkable
class VectorIndex(Protocol):
    """向量索引扩展协议。"""

    async def add(self, document: KnowledgeDocument, embedding: Sequence[float]) -> None:
        """将文档与其向量加入索引。"""
        ...

    async def search(
        self, query_embedding: Sequence[float], limit: int = 5
    ) -> tuple[SemanticMatch, ...]:
        """按余弦相似度降序检索文档。"""
        ...


class InMemoryVectorIndex:
    """基于纯 Python 余弦相似度的并发安全向量索引。"""

    def __init__(self) -> None:
        self._entries: dict[str, tuple[KnowledgeDocument, tuple[float, ...]]] = {}
        self._dimension: int | None = None
        self._lock = asyncio.Lock()

    async def add(self, document: KnowledgeDocument, embedding: Sequence[float]) -> None:
        """将文档与其向量加入索引。

        Args:
            document: 待索引的知识文档。
            embedding: 文档向量；维度必须与索引内已有向量一致。

        Raises:
            ValueError: 向量为空或与索引维度不一致。
        """
        vector = tuple(float(value) for value in embedding)
        if not vector:
            raise ValueError("embedding must not be empty")
        async with self._lock:
            if self._dimension is None:
                self._dimension = len(vector)
            elif len(vector) != self._dimension:
                raise ValueError(
                    f"embedding dimension mismatch: expected {self._dimension}, got {len(vector)}"
                )
            self._entries[document.document_id] = (document, vector)

    async def search(
        self, query_embedding: Sequence[float], limit: int = 5
    ) -> tuple[SemanticMatch, ...]:
        """按余弦相似度降序检索文档。

        Args:
            query_embedding: 查询向量；维度必须与索引内向量一致。
            limit: 返回结果数量上限。

        Returns:
            按相似度降序、文档标识升序排序的检索结果。

        Raises:
            ValueError: limit 小于一，或查询向量与索引维度不一致。
        """
        if limit < 1:
            raise ValueError("limit must be at least 1")
        query = tuple(float(value) for value in query_embedding)
        async with self._lock:
            if self._dimension is not None and len(query) != self._dimension:
                raise ValueError(
                    f"query embedding dimension mismatch: "
                    f"expected {self._dimension}, got {len(query)}"
                )
            entries = tuple(self._entries.values())
        matches = [
            SemanticMatch(document, _cosine_similarity(query, vector))
            for document, vector in entries
        ]
        matches.sort(key=lambda match: (-match.score, match.document.document_id))
        return tuple(matches[:limit])


class SemanticMemory:
    """组合 Embedder 与 VectorIndex 的语义记忆门面。"""

    def __init__(self, embedder: Embedder, index: VectorIndex | None = None) -> None:
        """初始化语义记忆。

        Args:
            embedder: 宿主注入的文本向量化实现。
            index: 向量索引实现；省略时使用内存索引。
        """
        self._embedder = embedder
        self._index: VectorIndex = index if index is not None else InMemoryVectorIndex()

    async def add_document(self, document: KnowledgeDocument) -> None:
        """向知识库添加文档并建立向量索引。

        Args:
            document: 待添加的知识文档。
        """
        embeddings = await self._embedder.embed([f"{document.title}\n{document.content}"])
        await self._index.add(document, embeddings[0])

    async def search(self, query: str, limit: int = 5) -> tuple[SemanticMatch, ...]:
        """按自然语言查询检索知识文档。

        Args:
            query: 查询文本。
            limit: 返回结果数量上限。

        Returns:
            按相似度降序排序的检索结果。
        """
        embeddings = await self._embedder.embed([query])
        return await self._index.search(embeddings[0], limit)


@dataclass(frozen=True, slots=True)
class KnowledgeTriple:
    """表示知识图谱中的一条三元组。"""

    subject: str
    predicate: str
    object: str
    metadata: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        """校验三元组各分量非空并冻结元数据。"""
        if not self.subject.strip():
            raise ValueError("subject must not be empty")
        if not self.predicate.strip():
            raise ValueError("predicate must not be empty")
        if not self.object.strip():
            raise ValueError("object must not be empty")
        object.__setattr__(self, "metadata", MappingProxyType(dict(self.metadata)))


@runtime_checkable
class KnowledgeGraph(Protocol):
    """知识图谱扩展协议。"""

    async def add(self, triple: KnowledgeTriple) -> None:
        """新增一条三元组。"""
        ...

    async def query(
        self,
        subject: str | None = None,
        predicate: str | None = None,
        object: str | None = None,
    ) -> tuple[KnowledgeTriple, ...]:
        """按任意分量组合过滤三元组。"""
        ...

    async def neighbors(self, entity: str) -> tuple[str, ...]:
        """返回与实体直接相连的全部实体。"""
        ...


class InMemoryKnowledgeGraph:
    """并发安全的知识图谱内存实现。"""

    def __init__(self) -> None:
        self._triples: dict[tuple[str, str, str], KnowledgeTriple] = {}
        self._lock = asyncio.Lock()

    async def add(self, triple: KnowledgeTriple) -> None:
        """新增一条三元组；相同主谓宾的三元组会被替换。

        Args:
            triple: 待写入的知识三元组。
        """
        async with self._lock:
            self._triples[(triple.subject, triple.predicate, triple.object)] = triple

    async def query(
        self,
        subject: str | None = None,
        predicate: str | None = None,
        object: str | None = None,
    ) -> tuple[KnowledgeTriple, ...]:
        """按任意分量组合过滤三元组。

        Args:
            subject: 主语过滤条件；None 表示不过滤。
            predicate: 谓语过滤条件；None 表示不过滤。
            object: 宾语过滤条件；None 表示不过滤。

        Returns:
            满足全部给定条件的三元组，按主谓宾字典序排序。
        """
        async with self._lock:
            triples = tuple(self._triples.values())
        selected = [
            triple
            for triple in triples
            if (subject is None or triple.subject == subject)
            and (predicate is None or triple.predicate == predicate)
            and (object is None or triple.object == object)
        ]
        selected.sort(key=lambda triple: (triple.subject, triple.predicate, triple.object))
        return tuple(selected)

    async def neighbors(self, entity: str) -> tuple[str, ...]:
        """返回与实体直接相连的全部实体。

        Args:
            entity: 作为主语或宾语出现的实体名。

        Returns:
            去重并按字典序排序的相邻实体。
        """
        async with self._lock:
            triples = tuple(self._triples.values())
        connected = {triple.object for triple in triples if triple.subject == entity}
        connected.update(triple.subject for triple in triples if triple.object == entity)
        connected.discard(entity)
        return tuple(sorted(connected))


def _cosine_similarity(left: Sequence[float], right: Sequence[float]) -> float:
    """计算两个等长向量的余弦相似度，零向量记为零。"""
    dot = sum(a * b for a, b in zip(left, right, strict=True))
    left_norm = math.sqrt(sum(a * a for a in left))
    right_norm = math.sqrt(sum(b * b for b in right))
    if left_norm == 0 or right_norm == 0:
        return 0.0
    return dot / (left_norm * right_norm)
