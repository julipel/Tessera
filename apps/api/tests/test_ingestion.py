"""Ingestion-пайплайн на фейковом коннекторе: статусы, дедупликация, удаления, изоляция."""

from dataclasses import dataclass, field, replace
from decimal import Decimal
from typing import Any
from uuid import UUID

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.knowledge.public import (
    ChunkRepository,
    DocumentItem,
    DocumentRepository,
    EntityItem,
    EntityRepository,
    Listing,
    MarkdownChunker,
    RawItem,
    RawItemRef,
    SourceConnector,
    SourceKind,
    SourceRecord,
    SourceRepository,
    SourceSpec,
    SourceSyncRecord,
    SourceSyncRepository,
    SqlSyncStore,
    SyncStatus,
    content_hash,
    run_sync,
)
from app.modules.shared.public import NotFoundError, TenantId, TenantMismatchError
from app.modules.tenants.public import SqlTenantDirectory


class FetchError(Exception):
    pass


@dataclass
class FakeConnector:
    """Источник в памяти. `changes` — что вернёт changed_since; `cursor` — новый курсор."""

    items: dict[str, RawItem] = field(default_factory=dict)
    kind: SourceKind = SourceKind.FILE
    cursor: str | None = None
    changes: list[str] = field(default_factory=list)
    broken: set[str] = field(default_factory=set)
    discover_error: Exception | None = None
    calls: list[tuple[str, Any]] = field(default_factory=list)

    async def discover(self, source: SourceSpec) -> Listing:
        self.calls.append(("discover", source.config))
        if self.discover_error is not None:
            raise self.discover_error
        return Listing([RawItemRef(ext) for ext in self.items], self.cursor)

    async def fetch(self, source: SourceSpec, ref: RawItemRef) -> RawItem:
        if ref.external_id in self.broken:
            raise FetchError("источник не ответил")
        return self.items[ref.external_id]

    async def changed_since(self, source: SourceSpec, cursor: str) -> Listing:
        self.calls.append(("changed_since", cursor))
        return Listing([RawItemRef(ext) for ext in self.changes], self.cursor)

    def put(self, *items: RawItem) -> None:
        for item in items:
            self.items[item.external_id] = item


def doc(ext: str, text: str = "Первый абзац.\n\nВторой абзац.") -> DocumentItem:
    return DocumentItem(external_id=ext, title=f"Документ {ext}", text=text, url=f"/{ext}")


def entity(ext: str, price: str = "990.00") -> EntityItem:
    return EntityItem(
        external_id=ext,
        type="product",
        title=f"Товар {ext}",
        price=Decimal(price),
        currency="RUB",
        in_stock=True,
        attributes={"volume_ml": 50},
    )


@dataclass
class Env:
    session: AsyncSession
    tenant_id: TenantId
    source_id: UUID
    connector: FakeConnector

    async def sync(self, *, full: bool = False, connector: SourceConnector | None = None) -> UUID:
        sync = await SourceSyncRepository(self.session).add(
            self.tenant_id, SourceSyncRecord(tenant_id=self.tenant_id, source_id=self.source_id)
        )
        sync_id = sync.id  # rollback внутри run_sync протухает ORM-объекты сессии
        await run_sync(
            self.tenant_id,
            sync_id,
            SqlSyncStore(self.session),
            {SourceKind.FILE: connector or self.connector},
            MarkdownChunker(max_tokens=2, overlap_tokens=0),
            full=full,
        )
        return sync_id

    async def sync_record(self, sync_id: UUID) -> SourceSyncRecord:
        record = await SourceSyncRepository(self.session).get_or_raise(self.tenant_id, sync_id)
        await self.session.refresh(record)
        return record

    async def documents(self) -> dict[str, Any]:
        rows = await DocumentRepository(self.session).list(self.tenant_id)
        return {r.external_id: r for r in rows}

    async def entities(self) -> dict[str, Any]:
        rows = await EntityRepository(self.session).list(self.tenant_id)
        return {r.external_id: r for r in rows}

    async def chunk_texts(self, external_id: str) -> list[str]:
        document = (await self.documents())[external_id]
        chunks = await ChunkRepository(self.session).list_for(self.tenant_id, document.id)
        return [c.text for c in chunks]


async def _env(session: AsyncSession, slug: str = "tenant-a") -> Env:
    tenant_id = (await SqlTenantDirectory(session).create(slug, slug)).id
    source = await SourceRepository(session).add(
        tenant_id, SourceRecord(tenant_id=tenant_id, kind=SourceKind.FILE, config={"path": slug})
    )
    return Env(session, tenant_id, source.id, FakeConnector())


@pytest.fixture
async def env(db_session: AsyncSession) -> Env:
    return await _env(db_session)


def stats_counts(record: SourceSyncRecord) -> dict[str, int]:
    keys = ("discovered", "created", "updated", "unchanged", "deleted", "failed")
    return {k: record.stats[k] for k in keys}


async def test_first_sync_creates_documents_entities_and_chunks(env: Env) -> None:
    env.connector.put(doc("faq"), entity("sku-1"))

    sync_id = await env.sync()

    record = await env.sync_record(sync_id)
    assert record.status is SyncStatus.SUCCEEDED
    assert record.started_at is not None
    assert record.finished_at is not None
    assert record.error is None
    assert stats_counts(record) == {
        "discovered": 2,
        "created": 2,
        "updated": 0,
        "unchanged": 0,
        "deleted": 0,
        "failed": 0,
    }
    document = (await env.documents())["faq"]
    assert (document.title, document.url) == ("Документ faq", "/faq")
    assert document.content_hash == content_hash(doc("faq"))
    assert await env.chunk_texts("faq") == ["Первый абзац.", "Второй абзац."]
    product = (await env.entities())["sku-1"]
    assert (product.price, product.attributes) == (Decimal("990.00"), {"volume_ml": 50})


async def test_unchanged_items_are_not_rewritten(env: Env) -> None:
    env.connector.put(doc("faq"), entity("sku-1"))
    await env.sync()
    chunk_ids = [c.id for c in await ChunkRepository(env.session).list(env.tenant_id)]
    updated_at = (await env.documents())["faq"].updated_at

    record = await env.sync_record(await env.sync())

    assert stats_counts(record)["unchanged"] == 2
    assert stats_counts(record)["created"] + stats_counts(record)["updated"] == 0
    assert [c.id for c in await ChunkRepository(env.session).list(env.tenant_id)] == chunk_ids
    document = (await env.documents())["faq"]
    await env.session.refresh(document)
    assert document.updated_at == updated_at


async def test_changed_items_are_updated_and_chunks_replaced(env: Env) -> None:
    env.connector.put(doc("faq"), entity("sku-1"))
    await env.sync()
    env.connector.put(doc("faq", text="Новый текст."), entity("sku-1", price="1200.00"))

    record = await env.sync_record(await env.sync())

    assert stats_counts(record)["updated"] == 2
    assert await env.chunk_texts("faq") == ["Новый текст."]
    product = (await env.entities())["sku-1"]
    await env.session.refresh(product)
    assert product.price == Decimal("1200.00")
    assert product.content_hash == content_hash(entity("sku-1", price="1200.00"))


async def test_full_sync_deletes_items_missing_from_source(env: Env) -> None:
    env.connector.put(doc("faq"), doc("old"), entity("sku-1"), entity("sku-gone"))
    await env.sync()
    del env.connector.items["old"], env.connector.items["sku-gone"]

    record = await env.sync_record(await env.sync())

    assert stats_counts(record)["deleted"] == 2
    assert set(await env.documents()) == {"faq"}
    assert set(await env.entities()) == {"sku-1"}
    remaining = await ChunkRepository(env.session).list(env.tenant_id)
    assert {c.document_id for c in remaining} == {(await env.documents())["faq"].id}


async def test_failed_item_does_not_stop_sync_and_is_not_deleted(env: Env) -> None:
    env.connector.put(doc("faq"), doc("flaky"))
    await env.sync()
    env.connector.put(doc("faq", text="Обновлено."))
    env.connector.broken = {"flaky"}

    record = await env.sync_record(await env.sync())

    assert record.status is SyncStatus.SUCCEEDED
    assert stats_counts(record)["failed"] == 1
    assert stats_counts(record)["updated"] == 1
    assert record.stats["errors"] == ["flaky: FetchError: источник не ответил"]
    assert set(await env.documents()) == {"faq", "flaky"}
    assert await env.chunk_texts("faq") == ["Обновлено."]


async def test_discover_error_marks_sync_failed(env: Env) -> None:
    env.connector.discover_error = RuntimeError("sitemap недоступен")

    record = await env.sync_record(await env.sync())

    assert record.status is SyncStatus.FAILED
    assert record.error == "RuntimeError: sitemap недоступен"
    assert record.finished_at is not None
    assert record.cursor is None


async def test_unknown_source_kind_marks_sync_failed(env: Env) -> None:
    sync = await SourceSyncRepository(env.session).add(
        env.tenant_id, SourceSyncRecord(tenant_id=env.tenant_id, source_id=env.source_id)
    )
    await run_sync(env.tenant_id, sync.id, SqlSyncStore(env.session), {}, MarkdownChunker())

    record = await env.sync_record(sync.id)
    assert record.status is SyncStatus.FAILED
    assert record.error is not None
    assert record.error.startswith("NoConnectorError")


async def test_incremental_sync_uses_cursor_and_keeps_unlisted_items(env: Env) -> None:
    env.connector.put(doc("faq"), doc("delivery"))
    env.connector.cursor = "c1"
    first = await env.sync_record(await env.sync())
    assert first.cursor == "c1"

    env.connector.put(doc("delivery", text="Доставка завтра."))
    env.connector.changes = ["delivery"]
    env.connector.cursor = "c2"
    record = await env.sync_record(await env.sync())

    assert env.connector.calls[-1] == ("changed_since", "c1")
    assert record.stats["incremental"] is True
    assert stats_counts(record) == {
        "discovered": 1,
        "created": 0,
        "updated": 1,
        "unchanged": 0,
        "deleted": 0,
        "failed": 0,
    }
    assert record.cursor == "c2"
    assert set(await env.documents()) == {"faq", "delivery"}


async def test_full_flag_ignores_cursor(env: Env) -> None:
    env.connector.put(doc("faq"))
    env.connector.cursor = "c1"
    await env.sync()

    record = await env.sync_record(await env.sync(full=True))

    assert env.connector.calls[-1] == ("discover", {"path": "tenant-a"})
    assert record.stats["incremental"] is False


async def test_incremental_without_new_cursor_keeps_previous(env: Env) -> None:
    env.connector.put(doc("faq"))
    env.connector.cursor = "c1"
    await env.sync()
    env.connector.cursor = None

    record = await env.sync_record(await env.sync())

    assert record.cursor == "c1"


async def test_finished_sync_is_not_rerun(env: Env) -> None:
    env.connector.put(doc("faq"))
    sync_id = await env.sync()
    env.connector.calls.clear()

    await run_sync(
        env.tenant_id,
        sync_id,
        SqlSyncStore(env.session),
        {SourceKind.FILE: env.connector},
        MarkdownChunker(),
    )

    assert env.connector.calls == []


async def test_sync_of_other_tenant_is_not_found(db_session: AsyncSession) -> None:
    a = await _env(db_session, "tenant-a")
    b = await _env(db_session, "tenant-b")
    b_sync = await SourceSyncRepository(db_session).add(
        b.tenant_id, SourceSyncRecord(tenant_id=b.tenant_id, source_id=b.source_id)
    )

    with pytest.raises(NotFoundError):
        await run_sync(
            a.tenant_id,
            b_sync.id,
            SqlSyncStore(db_session),
            {SourceKind.FILE: a.connector},
            MarkdownChunker(),
        )


async def test_sync_does_not_touch_other_tenant(db_session: AsyncSession) -> None:
    a = await _env(db_session, "tenant-a")
    b = await _env(db_session, "tenant-b")
    b.connector.put(doc("faq"))
    await b.sync()
    # Тот же external_id у тенанта A — отдельная запись; пустой листинг A не удаляет данные B.
    a.connector.put(doc("faq", text="Текст A."))
    await a.sync()
    a.connector.items.clear()
    await a.sync()

    assert set(await a.documents()) == set()
    assert set(await b.documents()) == {"faq"}
    assert await b.chunk_texts("faq") == ["Первый абзац.", "Второй абзац."]


async def test_upsert_into_foreign_source_is_rejected(db_session: AsyncSession) -> None:
    a = await _env(db_session, "tenant-a")
    b = await _env(db_session, "tenant-b")
    store = SqlSyncStore(db_session)
    await store.save_document(b.tenant_id, b.source_id, doc("faq"), "1" * 64, [])

    with pytest.raises(TenantMismatchError):
        await store.save_document(a.tenant_id, b.source_id, doc("faq"), "2" * 64, [])


# --- чистые функции ---


def test_content_hash_ignores_attribute_order() -> None:
    a = replace(entity("x"), attributes={"a": 1, "b": 2})
    b = replace(entity("x"), attributes={"b": 2, "a": 1})

    assert content_hash(a) == content_hash(b)
    assert content_hash(a) != content_hash(replace(a, price=Decimal("1.00")))


def test_fake_connector_satisfies_protocol() -> None:
    connector: SourceConnector = FakeConnector()
    assert connector.kind is SourceKind.FILE
