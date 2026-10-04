"""Репозитории модуля knowledge."""

from collections.abc import Collection, Sequence
from typing import Any
from uuid import UUID

from sqlalchemy import delete, func, select, update
from sqlalchemy.dialects.postgresql import insert

from app.modules.knowledge.domain.entities import SyncStatus
from app.modules.knowledge.domain.ingestion import ChunkDraft, DocumentItem, EntityItem
from app.modules.knowledge.infrastructure.models import (
    ChunkRecord,
    DocumentRecord,
    EntityRecord,
    SourceRecord,
    SourceSyncRecord,
)
from app.modules.shared.public import TenantId, TenantMismatchError, TenantRepository

_ACTIVE = SourceSyncRecord.status.in_([SyncStatus.PENDING, SyncStatus.RUNNING])


class SourceRepository(TenantRepository[SourceRecord]):
    model = SourceRecord


class SourceSyncRepository(TenantRepository[SourceSyncRecord]):
    model = SourceSyncRecord

    async def last_cursor(self, tenant_id: TenantId, source_id: UUID) -> str | None:
        stmt = (
            select(SourceSyncRecord.cursor)
            .where(
                SourceSyncRecord.tenant_id == tenant_id,
                SourceSyncRecord.source_id == source_id,
                SourceSyncRecord.status == SyncStatus.SUCCEEDED,
                SourceSyncRecord.cursor.is_not(None),
            )
            .order_by(SourceSyncRecord.finished_at.desc())
            .limit(1)
        )
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def open_active(self, tenant_id: TenantId, source_id: UUID) -> UUID:
        """Активная (pending/running) синхронизация источника или новая pending.

        Гонку двух вызовов разрешает частичный уникальный индекс: проигравший INSERT
        ничего не вставляет и читает синхронизацию победителя. Принадлежность источника
        тенанту проверяет вызывающий код.
        """
        for _ in range(3):  # активная могла завершиться между INSERT и SELECT
            inserted = await self.session.execute(
                insert(SourceSyncRecord)
                .values(tenant_id=tenant_id, source_id=source_id, status=SyncStatus.PENDING)
                .on_conflict_do_nothing(
                    index_elements=[SourceSyncRecord.source_id], index_where=_ACTIVE
                )
                .returning(SourceSyncRecord.id)
            )
            if (sync_id := inserted.scalar_one_or_none()) is not None:
                return sync_id
            active = await self.session.execute(
                select(SourceSyncRecord.id).where(
                    SourceSyncRecord.tenant_id == tenant_id,
                    SourceSyncRecord.source_id == source_id,
                    _ACTIVE,
                )
            )
            if (sync_id := active.scalar_one_or_none()) is not None:
                return sync_id
        raise RuntimeError(f"не удалось открыть синхронизацию источника {source_id}")

    async def set_fields(self, tenant_id: TenantId, sync_id: UUID, **values: Any) -> None:
        stmt = (
            update(SourceSyncRecord)
            .where(SourceSyncRecord.tenant_id == tenant_id, SourceSyncRecord.id == sync_id)
            .values(**values)
        )
        await self.session.execute(stmt)


class _SourceItemRepository[ModelT: (DocumentRecord, EntityRecord)](TenantRepository[ModelT]):
    """Общее для документов и сущностей: external_id уникален в пределах source_id."""

    async def hashes(self, tenant_id: TenantId, source_id: UUID) -> dict[str, str]:
        stmt = select(self.model.external_id, self.model.content_hash).where(
            self.model.tenant_id == tenant_id, self.model.source_id == source_id
        )
        return {ext: digest for ext, digest in await self.session.execute(stmt)}

    async def delete_by_external_ids(
        self, tenant_id: TenantId, source_id: UUID, external_ids: Collection[str]
    ) -> int:
        # Чанки и документы-описания удаляются каскадом по FK.
        stmt = delete(self.model).where(
            self.model.tenant_id == tenant_id,
            self.model.source_id == source_id,
            self.model.external_id.in_(external_ids),
        )
        result = await self.session.execute(stmt)
        return int(result.rowcount)  # type: ignore[attr-defined]

    async def _upsert(self, tenant_id: TenantId, source_id: UUID, values: dict[str, Any]) -> UUID:
        insert_stmt = insert(self.model).values(tenant_id=tenant_id, source_id=source_id, **values)
        stmt = insert_stmt.on_conflict_do_update(
            index_elements=["source_id", "external_id"],
            # onupdate при ON CONFLICT не применяется — updated_at задаём явно.
            set_={**values, "updated_at": func.now()},
            # Источник другого тенанта не перезаписываем: строка не вернётся.
            where=self.model.tenant_id == tenant_id,
        ).returning(self.model.id)
        item_id = (await self.session.execute(stmt)).scalar_one_or_none()
        if item_id is None:
            raise TenantMismatchError(f"{self.model.__name__}: источник другого тенанта")
        return item_id


class DocumentRepository(_SourceItemRepository[DocumentRecord]):
    model = DocumentRecord

    async def upsert(
        self, tenant_id: TenantId, source_id: UUID, item: DocumentItem, content_hash: str
    ) -> UUID:
        return await self._upsert(
            tenant_id,
            source_id,
            {
                "external_id": item.external_id,
                "title": item.title,
                "url": item.url,
                "metadata": item.metadata,
                "content_hash": content_hash,
            },
        )


class EntityRepository(_SourceItemRepository[EntityRecord]):
    model = EntityRecord

    async def upsert(
        self, tenant_id: TenantId, source_id: UUID, item: EntityItem, content_hash: str
    ) -> UUID:
        return await self._upsert(
            tenant_id,
            source_id,
            {
                "external_id": item.external_id,
                "type": item.type,
                "title": item.title,
                "price": item.price,
                "currency": item.currency,
                "in_stock": item.in_stock,
                "category": item.category,
                "url": item.url,
                "image_url": item.image_url,
                "attributes": item.attributes,
                "content_hash": content_hash,
            },
        )


class ChunkRepository(TenantRepository[ChunkRecord]):
    model = ChunkRecord

    async def list_for(self, tenant_id: TenantId, document_id: UUID) -> Sequence[ChunkRecord]:
        stmt = (
            self._scoped(tenant_id)
            .where(ChunkRecord.document_id == document_id)
            .order_by(ChunkRecord.ord)
        )
        return (await self.session.execute(stmt)).scalars().all()

    async def replace_for_document(
        self, tenant_id: TenantId, document_id: UUID, chunks: Sequence[ChunkDraft]
    ) -> list[UUID]:
        """Заменить чанки документа; id новых чанков в порядке `chunks`."""
        await self.session.execute(
            delete(ChunkRecord).where(
                ChunkRecord.tenant_id == tenant_id, ChunkRecord.document_id == document_id
            )
        )
        if not chunks:
            return []
        rows = await self.session.execute(
            insert(ChunkRecord)
            .values(
                [
                    {
                        "tenant_id": tenant_id,
                        "document_id": document_id,
                        "ord": c.ord,
                        "section": c.section,
                        "text": c.text,
                        "token_count": c.token_count,
                    }
                    for c in chunks
                ]
            )
            .returning(ChunkRecord.ord, ChunkRecord.id)
        )
        # Порядок строк RETURNING в многострочном INSERT не гарантирован — сопоставляем по ord.
        ids_by_ord: dict[int, UUID] = {ord_: id_ for ord_, id_ in rows}
        return [ids_by_ord[c.ord] for c in chunks]
