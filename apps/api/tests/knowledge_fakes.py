"""Фейки индексации знаний для тестов пайплайна: эмбеддер и векторный индекс в памяти."""

from collections.abc import Collection, Sequence
from dataclasses import dataclass, field
from uuid import UUID

from app.modules.knowledge.public import (
    EmbeddingError,
    IndexedDocument,
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
