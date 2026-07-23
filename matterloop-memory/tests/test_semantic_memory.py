"""Semantic Memory 与知识图谱测试。"""

import asyncio
from collections.abc import Sequence

import pytest
from matterloop_memory import (
    InMemoryKnowledgeGraph,
    InMemoryVectorIndex,
    KnowledgeDocument,
    KnowledgeTriple,
    SemanticMemory,
)

_VOCABULARY = ("polymer", "simulation", "database", "lammps")


class _KeywordEmbedder:
    """基于关键词出现次数的确定性向量化实现。"""

    async def embed(self, texts: Sequence[str]) -> tuple[tuple[float, ...], ...]:
        """将每段文本映射为固定词表上的计数向量。"""
        return tuple(
            tuple(float(text.casefold().count(term)) for term in _VOCABULARY) for text in texts
        )


def test_semantic_memory_ranks_documents_by_cosine_similarity() -> None:
    """检索结果应按余弦相似度降序排序。"""

    async def scenario() -> None:
        memory = SemanticMemory(_KeywordEmbedder())
        best = KnowledgeDocument("聚合物模拟", "polymer simulation with lammps", source="docs")
        weaker = KnowledgeDocument("数据库", "database indexing polymer", source="database")
        await memory.add_document(best)
        await memory.add_document(weaker)

        matches = await memory.search("polymer simulation lammps", limit=2)

        assert [match.document.document_id for match in matches] == [
            best.document_id,
            weaker.document_id,
        ]
        assert matches[0].score > matches[1].score
        assert matches[0].score == pytest.approx(1.0)

    asyncio.run(scenario())


def test_vector_index_rejects_dimension_mismatch() -> None:
    """向量维度不一致时新增与检索都应抛出 ValueError。"""

    async def scenario() -> None:
        index = InMemoryVectorIndex()
        document = KnowledgeDocument("标题", "内容", source="docs")
        await index.add(document, (1.0, 0.0, 0.0))

        with pytest.raises(ValueError):
            await index.add(KnowledgeDocument("另一篇", "内容", source="docs"), (1.0, 0.0))
        with pytest.raises(ValueError):
            await index.search((1.0, 0.0), limit=1)
        with pytest.raises(ValueError):
            await index.add(KnowledgeDocument("空向量", "内容", source="docs"), ())

    asyncio.run(scenario())


def test_vector_index_scores_zero_vector_as_zero() -> None:
    """零向量查询应得到零分而非除零错误。"""

    async def scenario() -> None:
        index = InMemoryVectorIndex()
        await index.add(KnowledgeDocument("标题", "内容", source="docs"), (1.0, 2.0))

        matches = await index.search((0.0, 0.0), limit=1)
        assert len(matches) == 1
        assert matches[0].score == 0.0

    asyncio.run(scenario())


def test_knowledge_graph_supports_combined_filters() -> None:
    """query 应支持主谓宾任意组合过滤。"""

    async def scenario() -> None:
        graph = InMemoryKnowledgeGraph()
        await graph.add(KnowledgeTriple("PVC", "is_a", "polymer"))
        await graph.add(KnowledgeTriple("PVC", "has_property", "Tg"))
        await graph.add(KnowledgeTriple("PE", "is_a", "polymer"))

        assert len(await graph.query()) == 3
        assert len(await graph.query(subject="PVC")) == 2
        assert len(await graph.query(predicate="is_a")) == 2
        assert len(await graph.query(object="polymer")) == 2

        exact = await graph.query(subject="PVC", predicate="is_a", object="polymer")
        assert len(exact) == 1
        assert exact[0].subject == "PVC"
        assert await graph.query(subject="PVC", object="metal") == ()

    asyncio.run(scenario())


def test_knowledge_graph_neighbors_are_bidirectional() -> None:
    """neighbors 应覆盖实体作为主语与宾语两个方向。"""

    async def scenario() -> None:
        graph = InMemoryKnowledgeGraph()
        await graph.add(KnowledgeTriple("PVC", "is_a", "polymer"))
        await graph.add(KnowledgeTriple("PE", "is_a", "polymer"))
        await graph.add(KnowledgeTriple("polymer", "studied_by", "materials_science"))

        assert await graph.neighbors("polymer") == ("PE", "PVC", "materials_science")
        assert await graph.neighbors("PVC") == ("polymer",)
        assert await graph.neighbors("unknown") == ()

    asyncio.run(scenario())


def test_knowledge_document_and_triple_validate_fields() -> None:
    """文档与三元组必填字段应被校验。"""
    with pytest.raises(ValueError):
        KnowledgeDocument(" ", "内容", source="docs")
    with pytest.raises(ValueError):
        KnowledgeDocument("标题", " ", source="docs")
    with pytest.raises(ValueError):
        KnowledgeTriple(" ", "is_a", "polymer")
    with pytest.raises(ValueError):
        KnowledgeTriple("PVC", "is_a", " ")
