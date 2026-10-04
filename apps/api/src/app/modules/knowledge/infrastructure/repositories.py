"""Репозитории модуля knowledge."""

from collections.abc import Collection, Sequence
from typing import Any
from uuid import UUID

from sqlalchemy import ColumnElement, delete, func, or_, select, update
from sqlalchemy.dialects.postgresql import insert

from app.modules.knowledge.domain.catalog import CatalogEntity, CatalogPage, CatalogQuery
from app.modules.knowledge.domain.entities import SourceKind, SyncStatus
from app.modules.knowledge.domain.ingestion import ChunkDraft, DocumentItem, EntityItem
from app.modules.knowledge.domain.source_seed import RegisteredSource
from app.modules.knowledge.infrastructure.catalog_query import (
    any_word_tsquery,
    entity_tsvector,
    filter_criteria,
    order_by,
    to_catalog_entity,
)
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
    """Ещё и `SourceRegistry` — именованные источники из YAML тенанта (ADR-0019)."""

    model = SourceRecord

    async def get_by_name(self, tenant_id: TenantId, name: str) -> RegisteredSource | None:
        stmt = self._scoped(tenant_id).where(SourceRecord.name == name)
        record = (await self.session.execute(stmt)).scalar_one_or_none()
        return None if record is None else RegisteredSource(record.id, record.kind, record.config)

    async def create(
        self, tenant_id: TenantId, name: str, kind: SourceKind, config: dict[str, Any]
    ) -> UUID:
        record = SourceRecord(tenant_id=tenant_id, name=name, kind=kind, config=config)
        return (await self.add(tenant_id, record)).id

    async def update_config(
        self, tenant_id: TenantId, source_id: UUID, config: dict[str, Any]
    ) -> None:
        stmt = (
            update(SourceRecord)
            .where(SourceRecord.tenant_id == tenant_id, SourceRecord.id == source_id)
            .values(config=config)
        )
        await self.session.execute(stmt)

    async def names(self, tenant_id: TenantId) -> list[str]:
        stmt = select(SourceRecord.name).where(
            SourceRecord.tenant_id == tenant_id, SourceRecord.name.is_not(None)
        )
        return [name for name in (await self.session.execute(stmt)).scalars() if name]


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

    async def search(self, tenant_id: TenantId, query: CatalogQuery) -> CatalogPage:
        """Структурный поиск по каталогу (ADR-0003, ADR-0016): фильтры, полнотекстовый `query`
        (подходит любое из слов, ранг — `ts_rank`) и сортировка."""
        criteria = filter_criteria(query.filters)
        rank: ColumnElement[Any] | None = None
        text = (query.query or "").strip()
        if text:
            ts_query = any_word_tsquery(text)
            document = entity_tsvector()
            # Запрос из одних стоп-слов пуст: тогда он не фильтрует и не ранжирует.
            criteria.append(or_(func.numnode(ts_query) == 0, document.op("@@")(ts_query)))
            rank = func.ts_rank(document, ts_query)
        stmt = (
            self._scoped(tenant_id)
            .add_columns(func.count().over().label("total"))
            .where(*criteria)
            .order_by(*order_by(query.sort, rank))
            .limit(query.limit)
            .offset(query.offset)
        )
        rows = (await self.session.execute(stmt)).all()
        if rows:
            total = int(rows[0].total)
        elif query.offset == 0:
            total = 0
        else:  # страница за концом выдачи: окну нечего посчитать
            count = (
                select(func.count())
                .select_from(EntityRecord)
                .where(EntityRecord.tenant_id == tenant_id, *criteria)
            )
            total = int((await self.session.execute(count)).scalar_one())
        return CatalogPage(items=[to_catalog_entity(row[0]) for row in rows], total=total)

    async def get_catalog_entity(self, tenant_id: TenantId, id: UUID) -> CatalogEntity | None:
        record = await self.get(tenant_id, id)
        return to_catalog_entity(record) if record is not None else None


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
