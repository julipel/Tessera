"""Фейки индексации и поиска знаний: эмбеддер, векторный индекс и реранкер в памяти."""

from collections.abc import Collection, Sequence
from dataclasses import dataclass, field
from uuid import UUID

from app.modules.knowledge.public import (
    ChunkHit,
    EmbeddingError,
    IndexedDocument,
    RerankError,
    VectorIndexError,
)
from app.modules.shared.public import TenantId

type DocKey = tuple[TenantId, UUID, str]  # (тенант, источник, external_id)


@dataclass
class FakeEmbedder:
    """Детерминированные векторы `[len(text), 1.0]`; `fail` — следующий вызов падает."""

    dimensions: int = 2
    fail: bool = False
    calls: list[list[str]] = field(default_factory=list)

    async def embed(self, texts: Sequence[str]) -> list[list[float]]:
        if self.fail:
            raise EmbeddingError("провайдер эмбеддингов недоступен")
        self.calls.append(list(texts))
        return [[float(len(t)), 1.0] for t in texts]

    @property
    def texts(self) -> list[str]:
        return [t for call in self.calls for t in call]


@dataclass
class InMemoryChunkIndex:
    """Документы индекса по (тенант, источник, external_id); `fail_*` — имитация сбоя Qdrant."""

    documents: dict[DocKey, IndexedDocument] = field(default_factory=dict)
    fail_replace: bool = False
    fail_delete: bool = False
    ensured: bool = False
    searches: list[tuple[TenantId, str, list[float], int]] = field(default_factory=list)

    async def ensure_collection(self) -> None:
        self.ensured = True

    async def replace_document(
        self, tenant_id: TenantId, source_id: UUID, document: IndexedDocument
    ) -> None:
        if self.fail_replace:
            raise VectorIndexError("Qdrant недоступен")
        key = (tenant_id, source_id, document.external_id)
        if document.chunks:
            self.documents[key] = document
        else:
            self.documents.pop(key, None)

    async def delete_documents(
        self, tenant_id: TenantId, source_id: UUID, external_ids: Collection[str]
    ) -> None:
        if self.fail_delete:
            raise VectorIndexError("Qdrant недоступен")
        for external_id in external_ids:
            self.documents.pop((tenant_id, source_id, external_id), None)

    def external_ids(self, tenant_id: TenantId) -> set[str]:
        return {ext for (t, _, ext) in self.documents if t == tenant_id}

    def chunk_ids(self, tenant_id: TenantId, external_id: str) -> list[UUID]:
        [document] = [
            d for (t, _, ext), d in self.documents.items() if (t, ext) == (tenant_id, external_id)
        ]
        return [c.chunk_id for c in document.chunks]

    async def search(
        self, tenant_id: TenantId, text: str, dense: Sequence[float], limit: int
    ) -> list[ChunkHit]:
        """Чанки тенанта, в которых есть слова запроса; больше общих слов — выше."""
        self.searches.append((tenant_id, text, list(dense), limit))
        words = set(text.lower().split())
        scored = [
            (len(words & set(chunk.text.lower().split())), source_id, document, chunk)
            for (t, source_id, _), document in self.documents.items()
            if t == tenant_id
            for chunk in document.chunks
        ]
        scored = [item for item in scored if item[0] > 0]
        scored.sort(key=lambda item: -item[0])
        return [
            ChunkHit(
                chunk_id=chunk.chunk_id,
                document_id=document.document_id,
                source_id=source_id,
                title=document.title,
                text=chunk.text,
                score=float(score),
                url=document.url,
                section=chunk.section,
            )
            for score, source_id, document, chunk in scored[:limit]
        ]


@dataclass
class FakeReranker:
    """Возвращает заданный порядок `order` (по умолчанию — обратный); `fail` — RerankError."""

    order: list[int] | None = None
    fail: bool = False
    calls: list[tuple[str, list[str], int]] = field(default_factory=list)

    async def rerank(self, query: str, texts: Sequence[str], top_n: int) -> list[int]:
        self.calls.append((query, list(texts), top_n))
        if self.fail:
            raise RerankError("реранкер недоступен")
        order = self.order if self.order is not None else list(reversed(range(len(texts))))
        return order[:top_n]
