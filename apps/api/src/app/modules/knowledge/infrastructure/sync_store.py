"""SyncStore поверх Postgres (порт из domain/ports.py)."""

from collections.abc import Collection
from typing import Any
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.knowledge.domain.entities import SyncStatus
from app.modules.knowledge.domain.ingestion import (
    ChunkDraft,
    DocumentItem,
    EntityItem,
    ItemKey,
    ItemKind,
    SavedDocument,
    SyncJob,
)
from app.modules.knowledge.infrastructure.models import SourceRecord, SourceSyncRecord
from app.modules.knowledge.infrastructure.repositories import (
    ChunkRepository,
    DocumentRepository,
    EntityRepository,
    SourceRepository,
    SourceSyncRepository,
)
from app.modules.shared.public import TenantId


class SqlSyncStore:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session
        self.sources = SourceRepository(session)
        self.syncs = SourceSyncRepository(session)
        self.documents = DocumentRepository(session)
        self.entities = EntityRepository(session)
        self.chunks = ChunkRepository(session)

    async def load_job(self, tenant_id: TenantId, sync_id: UUID) -> SyncJob | None:
        stmt = (
            select(SourceSyncRecord, SourceRecord)
            .join(SourceRecord, SourceRecord.id == SourceSyncRecord.source_id)
            .where(
                SourceSyncRecord.tenant_id == tenant_id,
                SourceSyncRecord.id == sync_id,
                SourceRecord.tenant_id == tenant_id,
            )
        )
        row = (await self.session.execute(stmt)).one_or_none()
        if row is None:
            return None
        sync, source = row
        return SyncJob(
            sync_id=sync.id,
            source_id=source.id,
            status=sync.status,
            kind=source.kind,
            config=source.config,
        )

    async def open_sync(self, tenant_id: TenantId, source_id: UUID) -> UUID | None:
        if await self.sources.get(tenant_id, source_id) is None:
            return None
        return await self.syncs.open_active(tenant_id, source_id)

    async def last_cursor(self, tenant_id: TenantId, source_id: UUID) -> str | None:
        return await self.syncs.last_cursor(tenant_id, source_id)

    async def mark_running(self, tenant_id: TenantId, sync_id: UUID) -> None:
        await self.syncs.set_fields(
            tenant_id, sync_id, status=SyncStatus.RUNNING, started_at=func.now(), error=None
        )

    async def finish(
        self,
        tenant_id: TenantId,
        sync_id: UUID,
        status: SyncStatus,
        stats: dict[str, Any],
        cursor: str | None,
        error: str | None,
    ) -> None:
        await self.syncs.set_fields(
            tenant_id,
            sync_id,
            status=status,
            finished_at=func.now(),
            stats=stats,
            cursor=cursor,
            error=error,
        )

    async def item_hashes(self, tenant_id: TenantId, source_id: UUID) -> dict[ItemKey, str]:
        documents = await self.documents.hashes(tenant_id, source_id)
        entities = await self.entities.hashes(tenant_id, source_id)
        return {
            **{ItemKey(ItemKind.DOCUMENT, ext): h for ext, h in documents.items()},
            **{ItemKey(ItemKind.ENTITY, ext): h for ext, h in entities.items()},
        }

    async def save_document(
        self,
        tenant_id: TenantId,
        source_id: UUID,
        item: DocumentItem,
        content_hash: str,
        chunks: list[ChunkDraft],
    ) -> SavedDocument:
        document_id = await self.documents.upsert(tenant_id, source_id, item, content_hash)
        chunk_ids = await self.chunks.replace_for_document(tenant_id, document_id, chunks)
        return SavedDocument(document_id, chunk_ids)

    async def save_entity(
        self, tenant_id: TenantId, source_id: UUID, item: EntityItem, content_hash: str
    ) -> None:
        await self.entities.upsert(tenant_id, source_id, item, content_hash)

    async def delete_items(
        self, tenant_id: TenantId, source_id: UUID, keys: Collection[ItemKey]
    ) -> int:
        documents = [k.external_id for k in keys if k.kind is ItemKind.DOCUMENT]
        entities = [k.external_id for k in keys if k.kind is ItemKind.ENTITY]
        deleted = 0
        if documents:
            deleted += await self.documents.delete_by_external_ids(tenant_id, source_id, documents)
        if entities:
            deleted += await self.entities.delete_by_external_ids(tenant_id, source_id, entities)
        return deleted

    async def commit(self) -> None:
        await self.session.commit()

    async def rollback(self) -> None:
        await self.session.rollback()
