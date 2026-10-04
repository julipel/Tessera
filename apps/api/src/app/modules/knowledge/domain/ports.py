"""Порты ingestion и поиска: коннекторы источников, чанкер, хранилище, эмбеддинги, векторный
индекс и реранкер (реализации — вне domain)."""

from collections.abc import Collection, Sequence
from pathlib import Path
from typing import Any, Protocol
from uuid import UUID

from app.modules.knowledge.domain.entities import SourceKind, SyncStatus
from app.modules.knowledge.domain.indexing import ChunkHit, IndexedDocument
from app.modules.knowledge.domain.ingestion import (
    ChunkDraft,
    DocumentItem,
    EntityItem,
    ItemKey,
    Listing,
    RawItem,
    RawItemRef,
    SavedDocument,
    SourceSpec,
    SyncJob,
)
from app.modules.knowledge.domain.source_seed import MirrorStats, RegisteredSource
from app.modules.shared.kernel import TenantId


class SourceConnector(Protocol):
    """Коннектор источника (architecture.md §8, ADR-0012)."""

    kind: SourceKind

    async def discover(self, source: SourceSpec) -> Listing:
        """Полный список элементов источника: чего нет в листинге — удаляется."""
        ...

    async def fetch(self, source: SourceSpec, ref: RawItemRef) -> RawItem: ...

    async def changed_since(self, source: SourceSpec, cursor: str) -> Listing:
        """Элементы, изменённые после `cursor`. Удалений не сообщает."""
        ...


class Chunker(Protocol):
    def chunk(self, text: str) -> list[ChunkDraft]: ...


class SyncStore(Protocol):
    """Хранилище ingestion. Транзакцией управляет пайплайн через commit/rollback."""

    async def load_job(self, tenant_id: TenantId, sync_id: UUID) -> SyncJob | None: ...

    async def open_sync(self, tenant_id: TenantId, source_id: UUID) -> UUID | None:
        """Активная синхронизация источника или новая pending; None — источника у тенанта нет."""
        ...

    async def last_cursor(self, tenant_id: TenantId, source_id: UUID) -> str | None:
        """Курсор последней успешной синхронизации источника."""
        ...

    async def mark_running(self, tenant_id: TenantId, sync_id: UUID) -> None: ...

    async def finish(
        self,
        tenant_id: TenantId,
        sync_id: UUID,
        status: SyncStatus,
        stats: dict[str, Any],
        cursor: str | None,
        error: str | None,
    ) -> None: ...

    async def item_hashes(self, tenant_id: TenantId, source_id: UUID) -> dict[ItemKey, str]:
        """content_hash всех документов и сущностей источника."""
        ...

    async def save_document(
        self,
        tenant_id: TenantId,
        source_id: UUID,
        item: DocumentItem,
        content_hash: str,
        chunks: list[ChunkDraft],
    ) -> SavedDocument:
        """Upsert по external_id; чанки документа заменяются целиком."""
        ...

    async def save_entity(
        self, tenant_id: TenantId, source_id: UUID, item: EntityItem, content_hash: str
    ) -> None: ...

    async def delete_items(
        self, tenant_id: TenantId, source_id: UUID, keys: Collection[ItemKey]
    ) -> int: ...

    async def commit(self) -> None: ...

    async def rollback(self) -> None: ...


class SyncQueue(Protocol):
    """Очередь фоновых синхронизаций. Повторная постановка той же `sync_id` — без дубля."""

    async def enqueue(self, tenant_id: TenantId, sync_id: UUID, *, full: bool) -> None: ...


class Embedder(Protocol):
    """Dense-эмбеддинги текстов. Ошибки — `EmbeddingError`."""

    dimensions: int

    async def embed(self, texts: Sequence[str]) -> list[list[float]]:
        """Векторы в порядке `texts`, каждый длины `dimensions`."""
        ...


class ChunkIndex(Protocol):
    """Векторный индекс чанков (dense + sparse BM25). Каждая операция ограничена тенантом
    (ADR-0006): точки другого тенанта не видны и не изменяются. Ошибки — `VectorIndexError`.
    """

    async def ensure_collection(self) -> None:
        """Создать коллекцию и индексы payload, если их нет; проверить размерность."""
        ...

    async def replace_document(
        self, tenant_id: TenantId, source_id: UUID, document: IndexedDocument
    ) -> None:
        """Точки документа (`source_id`, `external_id`) становятся ровно `document.chunks`."""
        ...

    async def delete_documents(
        self, tenant_id: TenantId, source_id: UUID, external_ids: Collection[str]
    ) -> None: ...

    async def search(
        self, tenant_id: TenantId, text: str, dense: Sequence[float], limit: int
    ) -> list[ChunkHit]:
        """Гибридный поиск: dense по `dense` и BM25 по `text`, слияние RRF. Не больше
        `limit` чанков тенанта, лучшие первыми."""
        ...


class Reranker(Protocol):
    """Переранжирование кандидатов поиска по запросу. Ошибки — `RerankError`."""

    async def rerank(self, query: str, texts: Sequence[str], top_n: int) -> list[int]:
        """Индексы `texts`, самые релевантные первыми; не больше `top_n`."""
        ...


class SourceRegistry(Protocol):
    """Именованные источники тенанта (ADR-0019)."""

    async def get_by_name(self, tenant_id: TenantId, name: str) -> RegisteredSource | None: ...

    async def create(
        self, tenant_id: TenantId, name: str, kind: SourceKind, config: dict[str, Any]
    ) -> UUID: ...

    async def update_config(
        self, tenant_id: TenantId, source_id: UUID, config: dict[str, Any]
    ) -> None: ...

    async def names(self, tenant_id: TenantId) -> list[str]:
        """Имена всех именованных источников тенанта."""
        ...


class SourceFileStore(Protocol):
    def mirror(self, source: SourceSpec, from_dir: Path) -> MirrorStats:
        """Сделать каталог источника копией `from_dir`: новые и изменённые файлы копируются,
        лишние удаляются; скрытые файлы и симлинки пропускаются. Синхронный (файловый I/O)."""
        ...
