"""Модель данных knowledge: изоляция тенантов в репозиториях (ADR-0006) и ограничения схемы."""

from dataclasses import dataclass
from decimal import Decimal

import pytest
from sqlalchemy import exc, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.knowledge.public import (
    ChunkRecord,
    ChunkRepository,
    DocumentRecord,
    DocumentRepository,
    EntityRecord,
    EntityRepository,
    SourceKind,
    SourceRecord,
    SourceRepository,
    SourceStatus,
    SourceSyncRecord,
    SourceSyncRepository,
    SyncStatus,
)
from app.modules.shared.public import (
    TenantId,
    TenantMismatchError,
    TenantRepository,
    TenantScopedBase,
)
from app.modules.tenants.public import SqlTenantDirectory

HASH = "0" * 64


@dataclass(frozen=True)
class KnowledgeRows:
    tenant_id: TenantId
    source: SourceRecord
    sync: SourceSyncRecord
    entity: EntityRecord
    document: DocumentRecord
    chunk: ChunkRecord

    def by_model(self, model: type[TenantScopedBase]) -> TenantScopedBase:
        return {
            SourceRecord: self.source,
            SourceSyncRecord: self.sync,
            EntityRecord: self.entity,
            DocumentRecord: self.document,
            ChunkRecord: self.chunk,
        }[model]


async def _seed(session: AsyncSession, slug: str) -> KnowledgeRows:
    tenant_id = (await SqlTenantDirectory(session).create(slug, slug)).id
    source = await SourceRepository(session).add(
        tenant_id, SourceRecord(tenant_id=tenant_id, kind=SourceKind.FILE, config={"path": "x"})
    )
    sync = await SourceSyncRepository(session).add(
        tenant_id, SourceSyncRecord(tenant_id=tenant_id, source_id=source.id)
    )
    entity = await EntityRepository(session).add(
        tenant_id,
        EntityRecord(
            tenant_id=tenant_id,
            source_id=source.id,
            external_id="sku-1",
            type="product",
            title="Крем",
            content_hash=HASH,
        ),
    )
    document = await DocumentRepository(session).add(
        tenant_id,
        DocumentRecord(
            tenant_id=tenant_id,
            source_id=source.id,
            entity_id=entity.id,
            external_id="doc-1",
            title="Доставка",
            content_hash=HASH,
        ),
    )
    chunk = await ChunkRepository(session).add(
        tenant_id,
        ChunkRecord(
            tenant_id=tenant_id, document_id=document.id, ord=0, text="Текст", token_count=1
        ),
    )
    return KnowledgeRows(tenant_id, source, sync, entity, document, chunk)


REPOSITORIES: list[type[TenantRepository]] = [  # type: ignore[type-arg]
    SourceRepository,
    SourceSyncRepository,
    EntityRepository,
    DocumentRepository,
    ChunkRepository,
]


@pytest.mark.parametrize("repo_cls", REPOSITORIES, ids=lambda r: r.__name__)
async def test_repository_isolates_tenants(
    db_session: AsyncSession,
    repo_cls: type[TenantRepository],  # type: ignore[type-arg]
) -> None:
    a = await _seed(db_session, "tenant-a")
    b = await _seed(db_session, "tenant-b")
    repo = repo_cls(db_session)
    a_row, b_row = a.by_model(repo.model), b.by_model(repo.model)

    assert await repo.get(a.tenant_id, b_row.id) is None
    assert [r.id for r in await repo.list(a.tenant_id)] == [a_row.id]
    assert await repo.delete(a.tenant_id, b_row.id) is False
    assert await repo.get(b.tenant_id, b_row.id) is b_row


async def test_add_rejects_foreign_tenant_id(db_session: AsyncSession) -> None:
    a = await _seed(db_session, "tenant-a")
    b = await _seed(db_session, "tenant-b")

    with pytest.raises(TenantMismatchError):
        await SourceRepository(db_session).add(
            a.tenant_id, SourceRecord(tenant_id=b.tenant_id, kind=SourceKind.TABLE)
        )


async def test_defaults(db_session: AsyncSession) -> None:
    rows = await _seed(db_session, "tenant-a")
    await db_session.refresh(rows.source)
    await db_session.refresh(rows.sync)

    assert rows.source.status is SourceStatus.ACTIVE
    assert rows.source.created_at is not None
    assert rows.sync.status is SyncStatus.PENDING
    assert rows.sync.stats == {}
    assert rows.sync.started_at is None


async def test_entity_round_trip(db_session: AsyncSession) -> None:
    rows = await _seed(db_session, "tenant-a")
    repo = EntityRepository(db_session)
    entity = await repo.add(
        rows.tenant_id,
        EntityRecord(
            tenant_id=rows.tenant_id,
            source_id=rows.source.id,
            external_id="sku-2",
            type="product",
            title="Сыворотка",
            price=Decimal("1990.50"),
            currency="RUB",
            in_stock=True,
            category="Уход",
            attributes={"volume_ml": 30, "skin": ["dry", "normal"]},
            content_hash=HASH,
        ),
    )
    db_session.expunge_all()  # читаем из БД, а не из identity map

    loaded = await repo.get_or_raise(rows.tenant_id, entity.id)
    assert loaded.price == Decimal("1990.50")
    assert loaded.attributes == {"volume_ml": 30, "skin": ["dry", "normal"]}
    assert loaded.updated_at is not None


@pytest.mark.parametrize("model", [DocumentRecord, EntityRecord], ids=lambda m: m.__name__)
async def test_external_id_unique_within_source(
    db_session: AsyncSession, model: type[DocumentRecord | EntityRecord]
) -> None:
    rows = await _seed(db_session, "tenant-a")
    existing = rows.by_model(model)
    fields = {"type": "product"} if model is EntityRecord else {}

    db_session.add(
        model(
            tenant_id=rows.tenant_id,
            source_id=rows.source.id,
            external_id=existing.external_id,  # type: ignore[attr-defined]
            title="Дубль",
            content_hash=HASH,
            **fields,
        )
    )
    with pytest.raises(exc.IntegrityError):
        await db_session.flush()


async def test_external_id_may_repeat_in_other_source(db_session: AsyncSession) -> None:
    rows = await _seed(db_session, "tenant-a")
    other = await SourceRepository(db_session).add(
        rows.tenant_id, SourceRecord(tenant_id=rows.tenant_id, kind=SourceKind.WEBSITE)
    )

    await DocumentRepository(db_session).add(
        rows.tenant_id,
        DocumentRecord(
            tenant_id=rows.tenant_id,
            source_id=other.id,
            external_id=rows.document.external_id,
            content_hash=HASH,
        ),
    )


async def test_chunk_ord_unique_within_document(db_session: AsyncSession) -> None:
    rows = await _seed(db_session, "tenant-a")

    db_session.add(
        ChunkRecord(
            tenant_id=rows.tenant_id,
            document_id=rows.document.id,
            ord=rows.chunk.ord,
            text="Дубль",
            token_count=1,
        )
    )
    with pytest.raises(exc.IntegrityError):
        await db_session.flush()


async def test_deleting_source_cascades(db_session: AsyncSession) -> None:
    rows = await _seed(db_session, "tenant-a")
    other = await _seed(db_session, "tenant-b")

    assert await SourceRepository(db_session).delete(rows.tenant_id, rows.source.id) is True
    db_session.expunge_all()

    for model in (SourceSyncRecord, EntityRecord, DocumentRecord, ChunkRecord):
        ids = (await db_session.execute(select(model.id))).scalars().all()
        assert ids == [other.by_model(model).id], model.__name__


async def test_deleting_entity_removes_its_description_document(db_session: AsyncSession) -> None:
    rows = await _seed(db_session, "tenant-a")

    assert await EntityRepository(db_session).delete(rows.tenant_id, rows.entity.id) is True
    db_session.expunge_all()

    document_repo = DocumentRepository(db_session)
    assert await document_repo.get(rows.tenant_id, rows.document.id) is None
    assert await ChunkRepository(db_session).list(rows.tenant_id) == []
