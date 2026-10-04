"""KnowledgeSearch (P4-07a) на фейках: эмбеддинг запроса, глубина кандидатов, реранкинг."""

import uuid

import pytest

from app.modules.knowledge.application.search import RERANK_CANDIDATES_MIN
from app.modules.knowledge.public import (
    EmbeddingError,
    IndexedChunk,
    IndexedDocument,
    KnowledgeSearch,
)
from app.modules.shared.public import TenantId
from knowledge_fakes import FakeEmbedder, FakeReranker, InMemoryChunkIndex

TENANT = TenantId(uuid.uuid4())
OTHER = TenantId(uuid.uuid4())
SOURCE = uuid.uuid4()


def index_with(tenant_id: TenantId, *texts: str) -> InMemoryChunkIndex:
    index = InMemoryChunkIndex()
    add(index, tenant_id, *texts)
    return index


def add(index: InMemoryChunkIndex, tenant_id: TenantId, *texts: str) -> None:
    for n, text in enumerate(texts):
        external_id = f"doc{len(index.documents)}_{n}"
        chunk = IndexedChunk(chunk_id=uuid.uuid4(), ord=0, text=text, dense=[1.0, 1.0])
        document = IndexedDocument(
            document_id=uuid.uuid4(), external_id=external_id, title=text, chunks=[chunk]
        )
        index.documents[(tenant_id, SOURCE, external_id)] = document


async def test_embeds_query_and_returns_top_k_without_reranker() -> None:
    embedder = FakeEmbedder()
    index = index_with(TENANT, "доставка курьером", "доставка почтой", "доставка самовывоз")
    search = KnowledgeSearch(embedder, index, FakeReranker())

    hits = await search.search(TENANT, "доставка", top_k=2, rerank=False)

    assert embedder.calls == [["доставка"]]
    assert index.searches == [(TENANT, "доставка", [8.0, 1.0], 2)]
    assert [hit.text for hit in hits] == ["доставка курьером", "доставка почтой"]


async def test_rerank_takes_more_candidates_and_reorders() -> None:
    index = index_with(TENANT, "оплата картой", "оплата наличными", "оплата частями")
    reranker = FakeReranker(order=[2, 0, 1])
    search = KnowledgeSearch(FakeEmbedder(), index, reranker)

    hits = await search.search(TENANT, "оплата", top_k=2, rerank=True)

    assert index.searches[0][3] == RERANK_CANDIDATES_MIN
    assert reranker.calls == [
        ("оплата", ["оплата картой", "оплата наличными", "оплата частями"], 2)
    ]
    assert [hit.text for hit in hits] == ["оплата частями", "оплата картой"]


async def test_rerank_requested_but_not_configured_uses_rrf_order() -> None:
    index = index_with(TENANT, "возврат товара", "возврат денег")
    search = KnowledgeSearch(FakeEmbedder(), index)

    hits = await search.search(TENANT, "возврат", top_k=1, rerank=True)

    assert index.searches[0][3] == 1
    assert [hit.text for hit in hits] == ["возврат товара"]


async def test_reranker_failure_falls_back_to_rrf_order() -> None:
    index = index_with(TENANT, "гарантия год", "гарантия два года", "гарантия на услуги")
    search = KnowledgeSearch(FakeEmbedder(), index, FakeReranker(fail=True))

    hits = await search.search(TENANT, "гарантия", top_k=2, rerank=True)

    assert [hit.text for hit in hits] == ["гарантия год", "гарантия два года"]


async def test_reranker_bad_indices_are_ignored() -> None:
    index = index_with(TENANT, "бонусы", "бонусы за отзыв")
    search = KnowledgeSearch(FakeEmbedder(), index, FakeReranker(order=[7, 1, 1, -1]))

    hits = await search.search(TENANT, "бонусы", top_k=2, rerank=True)

    assert [hit.text for hit in hits] == ["бонусы за отзыв"]


async def test_reranker_empty_answer_falls_back_to_rrf_order() -> None:
    index = index_with(TENANT, "адрес магазина", "адрес склада")
    search = KnowledgeSearch(FakeEmbedder(), index, FakeReranker(order=[]))

    hits = await search.search(TENANT, "адрес", top_k=1, rerank=True)

    assert [hit.text for hit in hits] == ["адрес магазина"]


async def test_single_candidate_is_not_reranked() -> None:
    reranker = FakeReranker()
    search = KnowledgeSearch(FakeEmbedder(), index_with(TENANT, "контакты"), reranker)

    hits = await search.search(TENANT, "контакты", top_k=3, rerank=True)

    assert [hit.text for hit in hits] == ["контакты"]
    assert reranker.calls == []


async def test_searches_only_own_tenant() -> None:
    index = index_with(TENANT, "сертификат подарочный")
    add(index, OTHER, "сертификат чужой")
    search = KnowledgeSearch(FakeEmbedder(), index)

    hits = await search.search(TENANT, "сертификат", top_k=5, rerank=False)

    assert [hit.text for hit in hits] == ["сертификат подарочный"]


async def test_embedding_error_propagates() -> None:
    search = KnowledgeSearch(FakeEmbedder(fail=True), index_with(TENANT, "текст"))

    with pytest.raises(EmbeddingError):
        await search.search(TENANT, "текст", top_k=1, rerank=False)


async def test_rejects_non_positive_top_k() -> None:
    search = KnowledgeSearch(FakeEmbedder(), InMemoryChunkIndex())

    with pytest.raises(ValueError, match="top_k"):
        await search.search(TENANT, "текст", top_k=0, rerank=False)
