"""Источники тенанта из YAML (P4-10b, ADR-0019): разбор декларации, seed по имени, копирование
файлов в каталог источника, синхронизация в процессе (`app.cli sync`)."""

import os
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.knowledge.public import (
    DocumentRepository,
    FileConnector,
    InvalidSourceDeclarationError,
    LocalSourceFileStore,
    MarkdownChunker,
    MirrorStats,
    SeedAction,
    SourceDeclaration,
    SourceKind,
    SourceRecord,
    SourceRepository,
    SourceSpec,
    SourceSyncRepository,
    SqlSyncStore,
    SyncStatus,
    parse_source_declarations,
    run_sync_now,
    seed_sources,
    validate_source_config,
)
from app.modules.shared.public import TenantId
from app.modules.tenants.public import SqlTenantDirectory, load_tenant_spec
from knowledge_fakes import FakeEmbedder, InMemoryChunkIndex

TABLE_CONFIG: dict[str, Any] = {
    "entity_type": "product",
    "columns": {"external_id": "sku", "title": "name"},
}


def write(path: Path, data: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(data, encoding="utf-8")


async def _tenant(session: AsyncSession, slug: str = "shop") -> TenantId:
    return (await SqlTenantDirectory(session).create(slug, slug)).id


async def _seed(
    session: AsyncSession, tenant_id: TenantId, root: Path, *declarations: SourceDeclaration
) -> Any:
    return await seed_sources(
        tenant_id,
        declarations,
        registry=SourceRepository(session),
        files=LocalSourceFileStore(root),
        validate=validate_source_config,
    )


# --- разбор декларации ---


def test_parse_resolves_files_relative_to_yaml(tmp_path: Path) -> None:
    (tmp_path / "data" / "kb").mkdir(parents=True)
    raw: list[dict[str, Any]] = [
        {"name": "kb", "kind": "file", "files": "../data/kb"},
        {"name": "site", "kind": "website", "config": {"start_urls": ["https://x.ru"]}},
    ]

    kb, site = parse_source_declarations(raw, tmp_path / "tenants")

    assert kb == SourceDeclaration("kb", SourceKind.FILE, {}, (tmp_path / "data" / "kb"))
    assert site.files is None and site.config == {"start_urls": ["https://x.ru"]}


@pytest.mark.parametrize(
    ("raw", "message"),
    [
        ([{"name": "Kb!", "kind": "file"}], r"sources\[1\]"),
        ([{"name": "kb", "kind": "ftp"}], r"sources\[1\]"),
        ([{"name": "kb", "kind": "file", "extra": 1}], r"sources\[1\]"),
        ([{"name": "kb", "kind": "file"}, {"name": "kb", "kind": "table"}], "повторяется"),
        ([{"name": "kb", "kind": "file", "files": "nope"}], "каталог files не найден"),
        ([{"name": "api", "kind": "http_api", "files": "."}], "только для file и table"),
    ],
)
def test_parse_rejects_bad_declarations(
    tmp_path: Path, raw: list[dict[str, Any]], message: str
) -> None:
    with pytest.raises(InvalidSourceDeclarationError, match=message):
        parse_source_declarations(raw, tmp_path)


def test_tenant_spec_keeps_sources_raw() -> None:
    spec = load_tenant_spec(
        """
tenant: {slug: shop, name: Shop}
agent_config:
  assistant: {name: A, greeting: "Привет!", fallback_message: "Ошибка"}
  model: {primary: {provider: openai, name: m}}
  limits: {}
  prompt: {tenant: "Ты — консультант."}
  tools: {builtin: []}
sources:
  - {name: kb, kind: file, files: ../kb}
"""
    )
    assert spec.sources == [{"name": "kb", "kind": "file", "files": "../kb"}]
    assert "sources" not in spec.config_json()


@pytest.mark.parametrize(
    ("kind", "config", "message"),
    [
        (SourceKind.FILE, {"path": "/etc"}, "config должен быть пустым"),
        (SourceKind.TABLE, {"entity_type": "product"}, "неверный конфиг источника table"),
        (SourceKind.WEBSITE, {}, "неверный конфиг источника website"),
    ],
)
def test_validate_source_config(kind: SourceKind, config: dict[str, Any], message: str) -> None:
    validate_source_config(SourceKind.FILE, {})
    validate_source_config(SourceKind.TABLE, TABLE_CONFIG)
    with pytest.raises(ValueError, match=message):
        validate_source_config(kind, config)


# --- seed ---


async def test_seed_creates_updates_and_is_idempotent(
    db_session: AsyncSession, tmp_path: Path
) -> None:
    tenant_id = await _tenant(db_session)
    root = tmp_path / "storage"
    write(tmp_path / "kb" / "faq.md", "# FAQ")
    kb = SourceDeclaration("kb", SourceKind.FILE, {}, tmp_path / "kb")
    catalog = SourceDeclaration("catalog", SourceKind.TABLE, TABLE_CONFIG)

    first = await _seed(db_session, tenant_id, root, kb, catalog)
    again = await _seed(db_session, tenant_id, root, kb, catalog)
    changed = {**TABLE_CONFIG, "currency": "RUB"}
    updated = await _seed(
        db_session, tenant_id, root, SourceDeclaration("catalog", SourceKind.TABLE, changed)
    )

    assert [(s.name, s.action) for s in first.seeded] == [
        ("kb", SeedAction.CREATED),
        ("catalog", SeedAction.CREATED),
    ]
    assert first.seeded[0].files == MirrorStats(copied=1)
    assert [s.action for s in again.seeded] == [SeedAction.UNCHANGED] * 2
    assert again.seeded[0].files == MirrorStats(unchanged=1)
    assert [s.source_id for s in again.seeded] == [s.source_id for s in first.seeded]
    assert updated.seeded[0].action is SeedAction.UPDATED
    assert updated.undeclared == ["kb"]
    source = await SourceRepository(db_session).get(tenant_id, first.seeded[1].source_id)
    assert source is not None and source.config == changed and source.name == "catalog"
    spec = SourceSpec(tenant_id, first.seeded[0].source_id, {})
    assert (FileConnector(root).source_dir(spec) / "faq.md").read_text() == "# FAQ"


async def test_invalid_config_writes_nothing(db_session: AsyncSession, tmp_path: Path) -> None:
    tenant_id = await _tenant(db_session)
    good = SourceDeclaration("kb", SourceKind.FILE, {})
    bad = SourceDeclaration("catalog", SourceKind.TABLE, {"entity_type": "product"})

    with pytest.raises(InvalidSourceDeclarationError, match="'catalog'"):
        await _seed(db_session, tenant_id, tmp_path, good, bad)

    assert await SourceRepository(db_session).list(tenant_id) == []


async def test_kind_change_is_rejected(db_session: AsyncSession, tmp_path: Path) -> None:
    tenant_id = await _tenant(db_session)
    await _seed(db_session, tenant_id, tmp_path, SourceDeclaration("kb", SourceKind.FILE, {}))

    with pytest.raises(InvalidSourceDeclarationError, match="не меняется"):
        await _seed(
            db_session, tenant_id, tmp_path, SourceDeclaration("kb", SourceKind.TABLE, TABLE_CONFIG)
        )


async def test_source_names_are_per_tenant(db_session: AsyncSession, tmp_path: Path) -> None:
    a, b = await _tenant(db_session, "a"), await _tenant(db_session, "b")
    kb = SourceDeclaration("kb", SourceKind.FILE, {})

    [in_a] = (await _seed(db_session, a, tmp_path, kb)).seeded
    [in_b] = (await _seed(db_session, b, tmp_path, kb)).seeded

    assert in_a.source_id != in_b.source_id
    assert in_b.action is SeedAction.CREATED
    assert await SourceRepository(db_session).get_by_name(a, "missing") is None
    with pytest.raises(IntegrityError):
        async with db_session.begin_nested():
            record = SourceRecord(tenant_id=a, name="kb", kind=SourceKind.FILE)
            await SourceRepository(db_session).add(a, record)


def test_mirror_copies_changed_and_removes_extra(tmp_path: Path) -> None:
    source = tmp_path / "src"
    write(source / "a.md", "A")
    write(source / "sub" / "b.md", "B")
    write(source / ".hidden", "x")
    os.symlink(source / "a.md", source / "link.md")
    store = LocalSourceFileStore(tmp_path / "root")
    spec = SourceSpec(TenantId(uuid4()), uuid4(), {})
    target = tmp_path / "root" / str(spec.tenant_id) / str(spec.source_id)

    assert store.mirror(spec, source) == MirrorStats(copied=2)
    write(source / "a.md", "A2")
    write(target / "stale.md", "old")
    write(target / ".keep", "служебный файл не трогаем")

    assert store.mirror(spec, source) == MirrorStats(copied=1, removed=1, unchanged=1)
    assert sorted(p.relative_to(target).as_posix() for p in target.rglob("*") if p.is_file()) == [
        ".keep",
        "a.md",
        "sub/b.md",
    ]
    assert (target / "a.md").read_text() == "A2"


# --- синхронизация в процессе ---


async def test_run_sync_now_indexes_seeded_files(db_session: AsyncSession, tmp_path: Path) -> None:
    tenant_id = await _tenant(db_session)
    root = tmp_path / "storage"
    write(tmp_path / "kb" / "faq.md", "# Доставка\n\nКурьером за 2 дня.")
    [kb] = (
        await _seed(
            db_session,
            tenant_id,
            root,
            SourceDeclaration("kb", SourceKind.FILE, {}, tmp_path / "kb"),
        )
    ).seeded
    index = InMemoryChunkIndex()

    sync_id = await run_sync_now(
        tenant_id,
        kb.source_id,
        SqlSyncStore(db_session),
        {SourceKind.FILE: FileConnector(root)},
        MarkdownChunker(),
        embedder=FakeEmbedder(),
        index=index,
        full=True,
    )

    sync = await SourceSyncRepository(db_session).get_or_raise(tenant_id, sync_id)
    assert sync.status is SyncStatus.SUCCEEDED
    [document] = await DocumentRepository(db_session).list(tenant_id)
    assert document.external_id == "faq.md"
    assert index.external_ids(tenant_id) == {"faq.md"}
