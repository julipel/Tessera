"""QdrantChunkIndex (P4-06a) на настоящем Qdrant из `make up`: своя коллекция на тест."""

import os
import uuid
from collections.abc import AsyncIterator
from typing import Any
from uuid import UUID

import httpx
import pytest

from app.modules.knowledge.infrastructure.qdrant_index import (
    BM25_MODEL,
    BM25_OPTIONS,
    SPARSE_VECTOR,
)
from app.modules.knowledge.public import (
    ChunkIndex,
    IndexedChunk,
    IndexedDocument,
    QdrantChunkIndex,
    VectorIndexError,
    create_qdrant_index,
)
from app.modules.shared.public import TenantId
from app.settings import Settings

DIMENSIONS = 4
TENANT_A = TenantId(uuid.uuid4())
TENANT_B = TenantId(uuid.uuid4())
SOURCE = uuid.uuid4()


def qdrant_url() -> str:
    return os.environ.get("TEST_QDRANT_URL") or Settings(_env_file=None).qdrant_url


@pytest.fixture
async def index() -> AsyncIterator[QdrantChunkIndex]:
    index = create_qdrant_index(
        qdrant_url(), collection=f"test_{uuid.uuid4().hex}", dimensions=DIMENSIONS
    )
    try:
        await index.ensure_collection()
    except VectorIndexError as e:
        await index.aclose()
        pytest.fail(f"Qdrant недоступен ({e}). Запусти `make up`.", pytrace=False)
    yield index
    await index.http.delete(f"/collections/{index.collection}")
    await index.aclose()


def chunk(ord: int, text: str, *, dense: list[float] | None = None) -> IndexedChunk:
    return IndexedChunk(
        chunk_id=uuid.uuid4(),
        ord=ord,
        text=text,
        dense=dense or [1.0, float(ord), 0.5, 0.25],
        section="Доставка > Сроки" if ord == 0 else None,
    )


def document(external_id: str, *chunks: IndexedChunk) -> IndexedDocument:
    return IndexedDocument(
        document_id=uuid.uuid4(),
        external_id=external_id,
        title=f"Документ {external_id}",
        url=f"https://example.com/{external_id}",
        chunks=chunks,
    )


async def points(index: QdrantChunkIndex, *, vectors: bool = False) -> list[dict[str, Any]]:
    response = await index.http.post(
        f"/collections/{index.collection}/points/scroll",
        json={"limit": 100, "with_payload": True, "with_vector": vectors},
    )
    response.raise_for_status()
    found: list[dict[str, Any]] = response.json()["result"]["points"]
    return found


async def ids_of(index: QdrantChunkIndex, tenant_id: TenantId) -> set[UUID]:
    return {
        UUID(p["id"]) for p in await points(index) if p["payload"]["tenant_id"] == str(tenant_id)
    }


async def test_ensure_collection_is_idempotent(index: QdrantChunkIndex) -> None:
    await index.ensure_collection()

    response = await index.http.get(f"/collections/{index.collection}")
    info = response.json()["result"]
    assert info["config"]["params"]["vectors"]["dense"] == {
        "size": DIMENSIONS,
        "distance": "Cosine",
    }
    assert info["config"]["params"]["sparse_vectors"][SPARSE_VECTOR] == {"modifier": "idf"}
    schema = info["payload_schema"]
    assert set(schema) == {"tenant_id", "source_id", "external_id"}
    assert schema["tenant_id"]["params"]["is_tenant"] is True


async def test_ensure_collection_rejects_other_dimensions(index: QdrantChunkIndex) -> None:
    other = QdrantChunkIndex(index.http, collection=index.collection, dimensions=DIMENSIONS + 1)

    with pytest.raises(VectorIndexError, match="несовместима"):
        await other.ensure_collection()


async def test_replace_document_stores_vectors_and_payload(index: QdrantChunkIndex) -> None:
    port: ChunkIndex = index
    doc = document("delivery", chunk(0, "Доставка курьером по Москве"), chunk(1, "Самовывоз"))

    await port.replace_document(TENANT_A, SOURCE, doc)

    stored = {UUID(p["id"]): p for p in await points(index, vectors=True)}
    assert set(stored) == {c.chunk_id for c in doc.chunks}
    first = stored[doc.chunks[0].chunk_id]
    assert first["payload"] == {
        "tenant_id": str(TENANT_A),
        "source_id": str(SOURCE),
        "document_id": str(doc.document_id),
        "external_id": "delivery",
        "title": "Документ delivery",
        "url": "https://example.com/delivery",
        "section": "Доставка > Сроки",
        "ord": 0,
        "text": "Доставка курьером по Москве",
    }
    assert len(first["vector"]["dense"]) == DIMENSIONS
    assert first["vector"][SPARSE_VECTOR]["indices"]


async def test_bm25_matches_russian_word_forms(index: QdrantChunkIndex) -> None:
    doc = document("delivery", chunk(0, "Доставка курьером по Москве"), chunk(1, "Оплата картой"))
    await index.replace_document(TENANT_A, SOURCE, doc)

    response = await index.http.post(
        f"/collections/{index.collection}/points/query",
        json={
            "query": {"text": "доставкой", "model": BM25_MODEL, "options": BM25_OPTIONS},
            "using": SPARSE_VECTOR,
            "filter": {"must": [{"key": "tenant_id", "match": {"value": str(TENANT_A)}}]},
            "limit": 5,
        },
    )
    response.raise_for_status()

    found = [UUID(p["id"]) for p in response.json()["result"]["points"]]
    assert found == [doc.chunks[0].chunk_id]


async def test_replace_document_drops_old_points(index: QdrantChunkIndex) -> None:
    await index.replace_document(TENANT_A, SOURCE, document("faq", chunk(0, "a"), chunk(1, "b")))
    other = document("other", chunk(0, "c"))
    await index.replace_document(TENANT_A, SOURCE, other)

    updated = document("faq", chunk(0, "новый текст"))
    await index.replace_document(TENANT_A, SOURCE, updated)
    assert await ids_of(index, TENANT_A) == {updated.chunks[0].chunk_id, other.chunks[0].chunk_id}

    await index.replace_document(TENANT_A, SOURCE, document("faq"))
    assert await ids_of(index, TENANT_A) == {other.chunks[0].chunk_id}


async def test_delete_documents_removes_only_listed(index: QdrantChunkIndex) -> None:
    docs = [document(ext, chunk(0, ext)) for ext in ("a", "b", "c")]
    for doc in docs:
        await index.replace_document(TENANT_A, SOURCE, doc)
    other_source = document("a", chunk(0, "a"))
    await index.replace_document(TENANT_A, uuid.uuid4(), other_source)

    await index.delete_documents(TENANT_A, SOURCE, ["a", "c", "missing"])
    await index.delete_documents(TENANT_A, SOURCE, [])

    assert await ids_of(index, TENANT_A) == {
        docs[1].chunks[0].chunk_id,
        other_source.chunks[0].chunk_id,
    }


async def test_tenant_isolation(index: QdrantChunkIndex) -> None:
    """Тот же source_id и external_id у другого тенанта не заменяются и не удаляются."""
    doc_a = document("shared", chunk(0, "тенант A"))
    doc_b = document("shared", chunk(0, "тенант B"), chunk(1, "тенант B, второй"))
    await index.replace_document(TENANT_A, SOURCE, doc_a)
    await index.replace_document(TENANT_B, SOURCE, doc_b)
    b_ids = {c.chunk_id for c in doc_b.chunks}

    await index.replace_document(TENANT_A, SOURCE, document("shared", chunk(0, "обновление A")))
    assert await ids_of(index, TENANT_B) == b_ids

    await index.delete_documents(TENANT_A, SOURCE, ["shared"])
    assert await ids_of(index, TENANT_A) == set()
    assert await ids_of(index, TENANT_B) == b_ids


async def test_rejected_request_raises(index: QdrantChunkIndex) -> None:
    bad = document("bad", chunk(0, "текст", dense=[1.0, 2.0]))

    with pytest.raises(VectorIndexError, match="400"):
        await index.replace_document(TENANT_A, SOURCE, bad)


async def test_network_error_raises() -> None:
    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    http = httpx.AsyncClient(base_url="http://qdrant.test", transport=httpx.MockTransport(refuse))
    index = QdrantChunkIndex(http, collection="c", dimensions=DIMENSIONS)

    with pytest.raises(VectorIndexError, match="ConnectError"):
        await index.ensure_collection()
    await http.aclose()
