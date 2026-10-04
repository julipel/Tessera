"""Порты ingestion: коннекторы источников, чанкер, хранилище (реализации — вне domain)."""

from collections.abc import Collection
from typing import Any, Protocol
from uuid import UUID

from app.modules.knowledge.domain.entities import SourceKind, SyncStatus
from app.modules.knowledge.domain.ingestion import (
    ChunkDraft,
    DocumentItem,
    EntityItem,
    ItemKey,
    Listing,
    RawItem,
    RawItemRef,
    SourceSpec,
    SyncJob,
)
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
    ) -> None:
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
