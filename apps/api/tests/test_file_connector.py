"""Коннектор `file`: каталог источника, форматы MD/TXT, курсор, изоляция путей."""

import os
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.knowledge.public import (
    ChunkRepository,
    DocumentItem,
    DocumentRepository,
    FileConnector,
    MarkdownChunker,
    RawItemRef,
    SourceConnector,
    SourceFileError,
    SourceKind,
    SourceRecord,
    SourceRepository,
    SourceSpec,
    SourceSyncRecord,
    SourceSyncRepository,
    SqlSyncStore,
    SyncStatus,
    run_sync,
)
from app.modules.shared.public import TenantId
from app.modules.tenants.public import SqlTenantDirectory
from knowledge_fakes import FakeEmbedder, InMemoryChunkIndex


def spec() -> SourceSpec:
    return SourceSpec(TenantId(uuid4()), uuid4(), {})


def write(path: Path, data: str | bytes, mtime_ns: int | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(data, str):
        path.write_text(data, encoding="utf-8")
    else:
        path.write_bytes(data)
    if mtime_ns is not None:
        os.utime(path, ns=(mtime_ns, mtime_ns))


@pytest.fixture
def source() -> SourceSpec:
    return spec()


@pytest.fixture
def connector(tmp_path: Path) -> FileConnector:
    return FileConnector(tmp_path)


@pytest.fixture
def base(connector: FileConnector, source: SourceSpec) -> Path:
    path = connector.source_dir(source)
    path.mkdir(parents=True)
    return path


async def test_discover_lists_supported_files(
    connector: FileConnector, source: SourceSpec, base: Path
) -> None:
    write(base / "faq.md", "# FAQ", mtime_ns=1_000)
    write(base / "docs" / "Оферта.TXT", "текст", mtime_ns=3_000)
    write(base / "image.png", b"\x89PNG")
    write(base / ".hidden.md", "скрыт")
    write(base / ".git" / "notes.md", "скрыт")

    listing = await connector.discover(source)

    assert listing.refs == [RawItemRef("faq.md", "1000"), RawItemRef("docs/Оферта.TXT", "3000")]
    assert listing.cursor == "3000"


async def test_missing_source_dir_is_an_error(connector: FileConnector) -> None:
    with pytest.raises(SourceFileError):
        await connector.discover(spec())


async def test_changed_since_returns_newer_files(
    connector: FileConnector, source: SourceSpec, base: Path
) -> None:
    write(base / "old.md", "старый", mtime_ns=1_000)
    write(base / "new.md", "новый", mtime_ns=2_000)

    listing = await connector.changed_since(source, "1000")
    assert [r.external_id for r in listing.refs] == ["new.md"]
    assert listing.cursor == "2000"

    nothing = await connector.changed_since(source, "2000")
    assert (nothing.refs, nothing.cursor) == ([], "2000")


async def test_fetch_markdown_takes_title_from_h1(
    connector: FileConnector, source: SourceSpec, base: Path
) -> None:
    write(base / "guide" / "care.md", "﻿Вступление\r\n\r\n# Забота о коже\r\n\r\nВечером.")

    item = await connector.fetch(source, RawItemRef("guide/care.md"))

    assert item == DocumentItem(
        external_id="guide/care.md",
        title="Забота о коже",
        text="Вступление\n\n# Забота о коже\n\nВечером.",
        metadata={"path": "guide/care.md", "format": "md"},
    )


async def test_fetch_text_in_cp1251_uses_file_name_as_title(
    connector: FileConnector, source: SourceSpec, base: Path
) -> None:
    write(base / "Прайс.txt", "Стрижка — 1500 р.".encode("cp1251"))

    item = await connector.fetch(source, RawItemRef("Прайс.txt"))

    assert isinstance(item, DocumentItem)
    assert (item.title, item.text) == ("Прайс", "Стрижка — 1500 р.")
    assert item.metadata["format"] == "txt"


@pytest.mark.parametrize("external_id", ["../other/secret.md", "/etc/passwd.md", "missing.md"])
async def test_fetch_rejects_paths_outside_source(
    tmp_path: Path, connector: FileConnector, source: SourceSpec, base: Path, external_id: str
) -> None:
    write(base.parent / "other" / "secret.md", "чужое")

    with pytest.raises(SourceFileError):
        await connector.fetch(source, RawItemRef(external_id))


async def test_symlinks_are_not_followed(
    tmp_path: Path, connector: FileConnector, source: SourceSpec, base: Path
) -> None:
    outside = tmp_path / "outside"
    write(outside / "secret.md", "чужое")
    (base / "link.md").symlink_to(outside / "secret.md")
    (base / "linked_dir").symlink_to(outside, target_is_directory=True)

    assert (await connector.discover(source)).refs == []
    for external_id in ("link.md", "linked_dir/secret.md"):
        with pytest.raises(SourceFileError):
            await connector.fetch(source, RawItemRef(external_id))


async def test_fetch_rejects_unsupported_and_too_large_files(
    tmp_path: Path, source: SourceSpec
) -> None:
    connector = FileConnector(tmp_path, max_file_bytes=10)
    base = connector.source_dir(source)
    write(base / "image.png", b"\x89PNG")
    write(base / "big.md", "x" * 11)

    for external_id in ("image.png", "big.md"):
        with pytest.raises(SourceFileError):
            await connector.fetch(source, RawItemRef(external_id))


def test_satisfies_protocol(connector: FileConnector) -> None:
    proto: SourceConnector = connector
    assert proto.kind is SourceKind.FILE


async def test_sync_stores_documents_with_structural_chunks(
    tmp_path: Path, db_session: AsyncSession
) -> None:
    tenant_id = (await SqlTenantDirectory(db_session).create("files", "files")).id
    source = await SourceRepository(db_session).add(
        tenant_id, SourceRecord(tenant_id=tenant_id, kind=SourceKind.FILE, config={})
    )
    connector = FileConnector(tmp_path)
    base = connector.source_dir(SourceSpec(tenant_id, source.id, {}))
    write(base / "delivery.md", "# Доставка\n\n## Сроки\n\nДва дня.\n\n## Цена\n\nБесплатно.")
    write(base / "notes.txt", "Без заголовков.")
    sync = await SourceSyncRepository(db_session).add(
        tenant_id, SourceSyncRecord(tenant_id=tenant_id, source_id=source.id)
    )
    sync_id = sync.id

    await run_sync(
        tenant_id,
        sync_id,
        SqlSyncStore(db_session),
        {SourceKind.FILE: connector},
        MarkdownChunker(),
        embedder=FakeEmbedder(),
        index=InMemoryChunkIndex(),
    )

    record = await SourceSyncRepository(db_session).get_or_raise(tenant_id, sync_id)
    await db_session.refresh(record)
    assert record.status is SyncStatus.SUCCEEDED
    assert record.stats["created"] == 2
    documents = {d.external_id: d for d in await DocumentRepository(db_session).list(tenant_id)}
    assert documents["delivery.md"].title == "Доставка"
    assert documents["notes.txt"].title == "notes"
    chunks = await ChunkRepository(db_session).list_for(tenant_id, documents["delivery.md"].id)
    assert [(c.section, c.text) for c in chunks] == [
        ("Доставка > Сроки", "## Сроки\n\nДва дня."),
        ("Доставка > Цена", "## Цена\n\nБесплатно."),
    ]
