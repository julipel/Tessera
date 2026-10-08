"""Источники знаний в админке (P8-03a, P8-03b, ADR-0037): список, создание, синхронизация,
история, файлы источников file/table."""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.contracts import (
    SourceDetail,
    SourceFile,
    SourceFileList,
    SourceList,
    SourceSyncItem,
    SourceSyncList,
)
from app.modules.knowledge.application.source_admin import check_file_name
from app.modules.knowledge.domain.errors import InvalidSourceError
from app.modules.knowledge.domain.source_admin import FILE_SUFFIXES
from app.modules.knowledge.infrastructure.file_connector import (
    DEFAULT_MAX_FILE_BYTES as FILE_MAX_BYTES,
)
from app.modules.knowledge.infrastructure.file_parsers import PARSERS
from app.modules.knowledge.infrastructure.table_connector import (
    DEFAULT_MAX_FILE_BYTES as TABLE_MAX_BYTES,
)
from app.modules.knowledge.infrastructure.table_readers import READERS
from app.modules.knowledge.public import (
    DocumentItem,
    EntityItem,
    FileConnector,
    InvalidSourceDeclarationError,
    LocalSourceFileStore,
    MarkdownChunker,
    SourceDeclaration,
    SourceFileLimits,
    SourceKind,
    SourceRepository,
    SqlSyncStore,
    SyncStatus,
    run_sync_now,
    seed_sources,
    validate_source_config,
)
from app.modules.shared.public import AdminRole, TenantId
from app.modules.tenants.public import SqlTenantDirectory
from knowledge_fakes import FakeEmbedder, InMemoryChunkIndex

AdminLogin = Callable[..., Awaitable[dict[str, str]]]  # фикстура admin_login из conftest

WEBSITE_YAML = "start_urls: ['https://example.ru/']\nmax_pages: 50\n"
TABLE_CONFIG: dict[str, Any] = {
    "entity_type": "product",
    "columns": {"external_id": "sku", "title": "name"},
}


@dataclass
class FakeSyncQueue:
    jobs: list[tuple[TenantId, UUID, bool]] = field(default_factory=list)

    async def enqueue(self, tenant_id: TenantId, sync_id: UUID, *, full: bool) -> None:
        self.jobs.append((tenant_id, sync_id, full))


@pytest.fixture
def queue(app: FastAPI) -> FakeSyncQueue:
    app.state.sync_queue = FakeSyncQueue()
    return app.state.sync_queue  # type: ignore[no-any-return]


async def _tenant(session: AsyncSession, slug: str) -> TenantId:
    return (await SqlTenantDirectory(session).create(slug, slug)).id


def _url(tenant_id: TenantId, suffix: str = "") -> str:
    return f"/v1/admin/tenants/{tenant_id}/sources{suffix}"


@pytest.fixture
async def shop(db_session: AsyncSession) -> TenantId:
    return await _tenant(db_session, "shop")


@pytest.fixture
async def editor(admin_login: AdminLogin, shop: TenantId) -> dict[str, str]:
    return await admin_login("editor@example.com", roles={shop: AdminRole.EDITOR})


async def _create(
    client: AsyncClient,
    tenant_id: TenantId,
    headers: dict[str, str],
    name: str = "site",
    kind: str = "website",
    config_yaml: str = WEBSITE_YAML,
) -> Any:
    return await client.post(
        _url(tenant_id),
        json={"name": name, "kind": kind, "config_yaml": config_yaml},
        headers=headers,
    )


async def _yaml_source(session: AsyncSession, tenant_id: TenantId, tmp_path: Path) -> UUID:
    result = await seed_sources(
        tenant_id,
        [SourceDeclaration("catalog", SourceKind.TABLE, TABLE_CONFIG)],
        registry=SourceRepository(session),
        files=LocalSourceFileStore(tmp_path),
        validate=validate_source_config,
    )
    return result.seeded[0].source_id


# --- доступ ---


async def test_requires_session(db_client: AsyncClient, shop: TenantId) -> None:
    response = await db_client.get(_url(shop))
    assert response.status_code == 401


async def test_viewer_reads_but_cannot_change(
    db_client: AsyncClient,
    admin_login: AdminLogin,
    shop: TenantId,
    editor: dict[str, str],
    queue: FakeSyncQueue,
) -> None:
    created = SourceDetail.model_validate((await _create(db_client, shop, editor)).json())
    viewer = await admin_login("viewer@example.com", roles={shop: AdminRole.VIEWER})

    assert (await db_client.get(_url(shop), headers=viewer)).status_code == 200
    assert (await db_client.get(_url(shop, f"/{created.id}"), headers=viewer)).status_code == 200
    syncs = await db_client.get(_url(shop, f"/{created.id}/syncs"), headers=viewer)
    assert syncs.status_code == 200

    assert (await _create(db_client, shop, viewer, name="other")).status_code == 403
    response = await db_client.post(_url(shop, f"/{created.id}/sync"), headers=viewer)
    assert response.status_code == 403
    assert queue.jobs == []


async def test_other_tenant_is_invisible(
    db_client: AsyncClient,
    db_session: AsyncSession,
    admin_login: AdminLogin,
    shop: TenantId,
    editor: dict[str, str],
    queue: FakeSyncQueue,
) -> None:
    other = await _tenant(db_session, "other")
    stranger = await admin_login("stranger@example.com", roles={other: AdminRole.EDITOR})
    created = SourceDetail.model_validate((await _create(db_client, shop, editor)).json())

    assert (await db_client.get(_url(shop), headers=stranger)).status_code == 403
    # Источник shop по пути тенанта other — как несуществующий.
    for response in [
        await db_client.get(_url(other, f"/{created.id}"), headers=stranger),
        await db_client.get(_url(other, f"/{created.id}/syncs"), headers=stranger),
        await db_client.post(_url(other, f"/{created.id}/sync"), headers=stranger),
    ]:
        assert response.status_code == 404
        assert response.json()["error"]["code"] == "not_found"
    listed = SourceList.model_validate((await db_client.get(_url(other), headers=stranger)).json())
    assert listed.sources == []
    assert queue.jobs == []


# --- создание ---


async def test_create_website_source(
    db_client: AsyncClient, shop: TenantId, editor: dict[str, str]
) -> None:
    response = await _create(db_client, shop, editor)
    assert response.status_code == 201, response.text
    created = SourceDetail.model_validate(response.json())
    assert (created.name, created.kind, created.origin, created.status) == (
        "site",
        "website",
        "admin",
        "active",
    )
    assert (created.documents, created.entities, created.last_sync) == (0, 0, None)
    assert "start_urls:\n- https://example.ru/" in created.config_yaml

    listed = SourceList.model_validate((await db_client.get(_url(shop), headers=editor)).json())
    assert [s.id for s in listed.sources] == [created.id]


async def test_create_file_source_with_empty_config(
    db_client: AsyncClient, shop: TenantId, editor: dict[str, str]
) -> None:
    response = await _create(db_client, shop, editor, name="kb", kind="file", config_yaml="  ")
    assert response.status_code == 201, response.text
    assert SourceDetail.model_validate(response.json()).config_yaml == ""


@pytest.mark.parametrize(
    ("kind", "config_yaml", "loc", "message"),
    [
        ("http_api", "url: https://x.ru/", ["kind"], "объявляется в YAML тенанта"),
        ("database", "", ["kind"], "объявляется в YAML тенанта"),
        ("website", "start_urls: [", ["config_yaml"], "YAML: строка 1"),
        ("website", "- 1", ["config_yaml"], "конфиг источника — объект"),
        ("website", "max_pages: 10", ["config_yaml", "start_urls"], ""),
        ("website", WEBSITE_YAML + "unknown: 1", ["config_yaml", "unknown"], ""),
        ("file", "path: /etc", ["config_yaml"], "config должен быть пустым"),
    ],
    ids=["http_api", "database", "broken-yaml", "not-object", "missing", "extra-key", "file"],
)
async def test_create_rejects_invalid_input(
    db_client: AsyncClient,
    shop: TenantId,
    editor: dict[str, str],
    kind: str,
    config_yaml: str,
    loc: list[Any],
    message: str,
) -> None:
    response = await _create(db_client, shop, editor, kind=kind, config_yaml=config_yaml)
    assert response.status_code == 422, response.text
    error = response.json()["error"]
    assert error["code"] == "invalid_input"
    assert any(d["loc"] == loc and message in d["message"] for d in error["details"]), error
    listed = SourceList.model_validate((await db_client.get(_url(shop), headers=editor)).json())
    assert listed.sources == []


async def test_create_rejects_bad_name(
    db_client: AsyncClient, shop: TenantId, editor: dict[str, str]
) -> None:
    response = await _create(db_client, shop, editor, name="Мой сайт")
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid_input"


async def test_name_is_unique_with_yaml_sources(
    db_client: AsyncClient,
    db_session: AsyncSession,
    admin_login: AdminLogin,
    shop: TenantId,
    editor: dict[str, str],
    tmp_path: Path,
) -> None:
    await _yaml_source(db_session, shop, tmp_path)
    assert (await _create(db_client, shop, editor)).status_code == 201

    for name in ["site", "catalog"]:
        response = await _create(db_client, shop, editor, name=name)
        assert response.status_code == 422
        [detail] = response.json()["error"]["details"]
        assert detail["loc"] == ["name"]
        assert "уже занято" in detail["message"]

    # В другом тенанте имя свободно.
    other = await _tenant(db_session, "other")
    other_editor = await admin_login("other@example.com", roles={other: AdminRole.EDITOR})
    assert (await _create(db_client, other, other_editor)).status_code == 201


async def test_seed_does_not_take_over_admin_source(
    db_client: AsyncClient,
    db_session: AsyncSession,
    shop: TenantId,
    editor: dict[str, str],
    tmp_path: Path,
) -> None:
    assert (await _create(db_client, shop, editor)).status_code == 201
    with pytest.raises(InvalidSourceDeclarationError, match="занято источником из админки"):
        await seed_sources(
            shop,
            [SourceDeclaration("site", SourceKind.WEBSITE, {"start_urls": ["https://a.ru/"]})],
            registry=SourceRepository(db_session),
            files=LocalSourceFileStore(tmp_path),
            validate=validate_source_config,
        )


# --- синхронизация и история ---


async def test_sync_is_queued_once(
    db_client: AsyncClient, shop: TenantId, editor: dict[str, str], queue: FakeSyncQueue
) -> None:
    created = SourceDetail.model_validate((await _create(db_client, shop, editor)).json())

    first = await db_client.post(
        _url(shop, f"/{created.id}/sync"), json={"full": True}, headers=editor
    )
    assert first.status_code == 202, first.text
    run = SourceSyncItem.model_validate(first.json())
    assert (run.status, run.started_at, run.stats, run.error) == ("pending", None, None, None)
    # Повтор без тела — та же синхронизация; задача ставится снова (дубль снимет arq).
    again = await db_client.post(_url(shop, f"/{created.id}/sync"), headers=editor)
    assert SourceSyncItem.model_validate(again.json()).id == run.id
    assert queue.jobs == [(shop, run.id, True), (shop, run.id, False)]

    detail = SourceDetail.model_validate(
        (await db_client.get(_url(shop, f"/{created.id}"), headers=editor)).json()
    )
    assert detail.last_sync is not None and detail.last_sync.id == run.id


async def test_sync_unknown_source(
    db_client: AsyncClient, shop: TenantId, editor: dict[str, str], queue: FakeSyncQueue
) -> None:
    response = await db_client.post(_url(shop, f"/{uuid4()}/sync"), headers=editor)
    assert response.status_code == 404
    assert queue.jobs == []


async def test_yaml_source_can_be_synced(
    db_client: AsyncClient,
    db_session: AsyncSession,
    shop: TenantId,
    editor: dict[str, str],
    queue: FakeSyncQueue,
    tmp_path: Path,
) -> None:
    source_id = await _yaml_source(db_session, shop, tmp_path)
    detail = SourceDetail.model_validate(
        (await db_client.get(_url(shop, f"/{source_id}"), headers=editor)).json()
    )
    assert (detail.name, detail.origin) == ("catalog", "yaml")
    assert "entity_type: product" in detail.config_yaml
    response = await db_client.post(_url(shop, f"/{source_id}/sync"), headers=editor)
    assert response.status_code == 202
    assert len(queue.jobs) == 1


async def test_history_shows_stats_and_errors(
    db_client: AsyncClient,
    db_session: AsyncSession,
    shop: TenantId,
    editor: dict[str, str],
    queue: FakeSyncQueue,
) -> None:
    created = SourceDetail.model_validate((await _create(db_client, shop, editor)).json())
    store = SqlSyncStore(db_session)

    async def finished(status: SyncStatus, stats: dict[str, Any], error: str | None) -> UUID:
        sync_id = await store.open_sync(shop, created.id)
        assert sync_id is not None
        await store.mark_running(shop, sync_id)
        await store.finish(shop, sync_id, status, stats, None, error)
        await store.commit()
        return sync_id

    stats = {
        "discovered": 3,
        "created": 1,
        "updated": 0,
        "unchanged": 0,
        "deleted": 0,
        "failed": 1,
        "incremental": False,
        "errors": ["https://example.ru/a: WebsiteSourceError: 500"],
    }
    succeeded = await finished(SyncStatus.SUCCEEDED, stats, None)
    failed = await finished(
        SyncStatus.FAILED, {**stats, "discovered": 0, "failed": 0, "errors": []}, "robots: 503"
    )
    # Данные источника: документ и сущность с описанием (оно — не документ).
    await store.save_document(shop, created.id, DocumentItem("a", "A", "текст"), "h1", [])
    await store.save_entity(shop, created.id, EntityItem("p1", "product", "Крем"), "h2")
    await store.commit()
    pending = SourceSyncItem.model_validate(
        (await db_client.post(_url(shop, f"/{created.id}/sync"), headers=editor)).json()
    )

    response = await db_client.get(_url(shop, f"/{created.id}/syncs"), headers=editor)
    syncs = SourceSyncList.model_validate(response.json()).syncs
    assert [s.id for s in syncs] == [pending.id, failed, succeeded]
    assert syncs[0].stats is None
    assert syncs[1].status == "failed" and syncs[1].error == "robots: 503"
    assert syncs[2].stats is not None
    assert syncs[2].stats.failed == 1
    assert syncs[2].stats.errors == ["https://example.ru/a: WebsiteSourceError: 500"]
    assert syncs[2].started_at is not None and syncs[2].finished_at is not None

    [summary] = SourceList.model_validate(
        (await db_client.get(_url(shop), headers=editor)).json()
    ).sources
    assert (summary.documents, summary.entities) == (1, 1)
    assert summary.last_sync is not None and summary.last_sync.id == pending.id


# --- файлы источников file/table (P8-03b) ---


@pytest.fixture
def files_root(app: FastAPI, tmp_path: Path) -> Path:
    root = tmp_path / "knowledge"
    app.state.source_files = LocalSourceFileStore(root)
    return root


async def _file_source(
    client: AsyncClient, tenant_id: TenantId, headers: dict[str, str], kind: str = "file"
) -> UUID:
    config = (
        "" if kind == "file" else "entity_type: product\ncolumns: {external_id: sku, title: name}"
    )
    response = await _create(client, tenant_id, headers, name=kind, kind=kind, config_yaml=config)
    assert response.status_code == 201, response.text
    return SourceDetail.model_validate(response.json()).id


def _files_url(tenant_id: TenantId, source_id: UUID, name: str = "") -> str:
    return _url(tenant_id, f"/{source_id}/files" + (f"/{name}" if name else ""))


async def _upload(
    client: AsyncClient,
    tenant_id: TenantId,
    source_id: UUID,
    headers: dict[str, str],
    name: str,
    data: bytes,
) -> Any:
    return await client.post(
        _files_url(tenant_id, source_id, name),
        content=data,
        headers={**headers, "Content-Type": "application/octet-stream"},
    )


async def _file_names(
    client: AsyncClient, tenant_id: TenantId, source_id: UUID, headers: dict[str, str]
) -> list[str]:
    response = await client.get(_files_url(tenant_id, source_id), headers=headers)
    assert response.status_code == 200, response.text
    return [f.name for f in SourceFileList.model_validate(response.json()).files]


FAQ_V1 = "# FAQ\n\nОтвет.".encode()


async def test_upload_replace_delete_and_sync(
    db_client: AsyncClient,
    db_session: AsyncSession,
    shop: TenantId,
    editor: dict[str, str],
    files_root: Path,
) -> None:
    source_id = await _file_source(db_client, shop, editor)
    assert await _file_names(db_client, shop, source_id, editor) == []

    first = await _upload(db_client, shop, source_id, editor, "faq.md", FAQ_V1)
    assert first.status_code == 200, first.text
    uploaded = SourceFile.model_validate(first.json())
    assert (uploaded.name, uploaded.size) == ("faq.md", len(FAQ_V1))
    assert (files_root / str(shop) / str(source_id) / "faq.md").read_bytes() == FAQ_V1
    await _upload(db_client, shop, source_id, editor, "Доставка.txt", "Курьером.".encode())

    replaced = await _upload(
        db_client, shop, source_id, editor, "faq.md", "# FAQ\n\nНовый.".encode()
    )
    assert SourceFile.model_validate(replaced.json()).modified_at >= uploaded.modified_at
    assert await _file_names(db_client, shop, source_id, editor) == ["faq.md", "Доставка.txt"]
    # Временных файлов загрузки не остаётся.
    assert sorted(p.name for p in (files_root / str(shop) / str(source_id)).iterdir()) == [
        "faq.md",
        "Доставка.txt",
    ]

    # Загруженное читает коннектор file.
    index = InMemoryChunkIndex()
    await run_sync_now(
        shop,
        source_id,
        SqlSyncStore(db_session),
        {SourceKind.FILE: FileConnector(files_root)},
        MarkdownChunker(),
        embedder=FakeEmbedder(),
        index=index,
        full=True,
    )
    assert index.external_ids(shop) == {"faq.md", "Доставка.txt"}

    deleted = await db_client.delete(_files_url(shop, source_id, "faq.md"), headers=editor)
    assert deleted.status_code == 204
    assert await _file_names(db_client, shop, source_id, editor) == ["Доставка.txt"]
    again = await db_client.delete(_files_url(shop, source_id, "faq.md"), headers=editor)
    assert again.status_code == 404


async def test_table_source_accepts_only_tables(
    db_client: AsyncClient, shop: TenantId, editor: dict[str, str], files_root: Path
) -> None:
    source_id = await _file_source(db_client, shop, editor, kind="table")
    ok = await _upload(db_client, shop, source_id, editor, "price.CSV", b"sku;name\n1;A\n")
    assert ok.status_code == 200, ok.text
    wrong = await _upload(db_client, shop, source_id, editor, "faq.md", b"# FAQ")
    assert wrong.status_code == 422
    assert ".csv, .xlsx" in wrong.json()["error"]["message"]


@pytest.mark.parametrize(
    ("name", "data"),
    [
        (".hidden.md", b"x"),
        ("a%5Cb.md", b"x"),
        ("run.exe", b"x"),
        ("table.csv", b"x"),
        ("tab%09name.md", b"x"),
        ("x" * 253 + ".md", b"x"),
        ("empty.md", b""),
    ],
    ids=["hidden", "backslash", "exe", "csv-in-file", "control", "long", "empty"],
)
async def test_upload_rejects_bad_files(
    db_client: AsyncClient,
    shop: TenantId,
    editor: dict[str, str],
    files_root: Path,
    name: str,
    data: bytes,
) -> None:
    source_id = await _file_source(db_client, shop, editor)
    response = await _upload(db_client, shop, source_id, editor, name, data)
    assert response.status_code == 422, response.text
    assert await _file_names(db_client, shop, source_id, editor) == []


@pytest.mark.parametrize("name", ["..", ".", "a/b.md", "/a.md", "a\\b.md", ""])
def test_file_name_is_single_segment(name: str) -> None:
    # Через HTTP такие имена не дойдут (клиент нормализует путь), но проверка — в сценарии.
    with pytest.raises(InvalidSourceError, match="недопустимое имя"):
        check_file_name(SourceKind.FILE, name)


async def test_upload_limits(
    app: FastAPI,
    db_client: AsyncClient,
    shop: TenantId,
    editor: dict[str, str],
    files_root: Path,
) -> None:
    app.state.source_file_limits = SourceFileLimits(max_bytes=10, max_files=2)
    source_id = await _file_source(db_client, shop, editor)

    too_large = await _upload(db_client, shop, source_id, editor, "big.md", b"x" * 11)
    assert too_large.status_code == 413
    assert too_large.json()["error"]["code"] == "invalid_input"
    # Без Content-Length размер проверяется по ходу чтения.

    async def chunks() -> Any:
        for _ in range(3):
            yield b"xxxx"

    streamed = await db_client.post(
        _files_url(shop, source_id, "stream.md"), content=chunks(), headers=editor
    )
    assert streamed.status_code == 413

    for name in ["a.md", "b.md"]:
        assert (await _upload(db_client, shop, source_id, editor, name, b"x")).status_code == 200
    third = await _upload(db_client, shop, source_id, editor, "c.md", b"x")
    assert third.status_code == 422
    assert "уже 2 файлов" in third.json()["error"]["message"]
    # Замену существующего лимит числа файлов не запрещает.
    assert (await _upload(db_client, shop, source_id, editor, "a.md", b"y")).status_code == 200
    assert await _file_names(db_client, shop, source_id, editor) == ["a.md", "b.md"]


async def test_files_only_for_admin_file_sources(
    db_client: AsyncClient,
    db_session: AsyncSession,
    shop: TenantId,
    editor: dict[str, str],
    files_root: Path,
    tmp_path: Path,
) -> None:
    site = SourceDetail.model_validate((await _create(db_client, shop, editor)).json())
    listed = await db_client.get(_files_url(shop, site.id), headers=editor)
    assert listed.status_code == 422
    assert "нет файлов" in listed.json()["error"]["message"]

    # Источник из YAML: файлы видны, но меняет их только make seed.
    yaml_dir = tmp_path / "yaml-files"
    (yaml_dir).mkdir()
    (yaml_dir / "catalog.csv").write_text("sku;name\n1;A\n", encoding="utf-8")
    result = await seed_sources(
        shop,
        [SourceDeclaration("catalog", SourceKind.TABLE, TABLE_CONFIG, yaml_dir)],
        registry=SourceRepository(db_session),
        files=LocalSourceFileStore(files_root),
        validate=validate_source_config,
    )
    catalog = result.seeded[0].source_id
    assert await _file_names(db_client, shop, catalog, editor) == ["catalog.csv"]
    upload = await _upload(db_client, shop, catalog, editor, "more.csv", b"sku;name\n2;B\n")
    assert upload.status_code == 422
    assert "make seed" in upload.json()["error"]["message"]
    delete = await db_client.delete(_files_url(shop, catalog, "catalog.csv"), headers=editor)
    assert delete.status_code == 422


async def test_file_access_by_role_and_tenant(
    db_client: AsyncClient,
    db_session: AsyncSession,
    admin_login: AdminLogin,
    shop: TenantId,
    editor: dict[str, str],
    files_root: Path,
) -> None:
    source_id = await _file_source(db_client, shop, editor)
    await _upload(db_client, shop, source_id, editor, "faq.md", b"# FAQ")
    viewer = await admin_login("viewer@example.com", roles={shop: AdminRole.VIEWER})
    assert await _file_names(db_client, shop, source_id, viewer) == ["faq.md"]
    assert (await _upload(db_client, shop, source_id, viewer, "x.md", b"x")).status_code == 403
    response = await db_client.delete(_files_url(shop, source_id, "faq.md"), headers=viewer)
    assert response.status_code == 403

    other = await _tenant(db_session, "other")
    stranger = await admin_login("stranger@example.com", roles={other: AdminRole.EDITOR})
    for response in [
        await db_client.get(_files_url(other, source_id), headers=stranger),
        await _upload(db_client, other, source_id, stranger, "x.md", b"x"),
        await db_client.delete(_files_url(other, source_id, "faq.md"), headers=stranger),
    ]:
        assert response.status_code == 404
    assert await _file_names(db_client, shop, source_id, editor) == ["faq.md"]


def test_upload_suffixes_match_connectors() -> None:
    # Загрузить можно ровно то, что читают коннекторы file и table.
    assert FILE_SUFFIXES[SourceKind.FILE] == set(PARSERS)
    assert FILE_SUFFIXES[SourceKind.TABLE] == set(READERS)
    assert SourceFileLimits().max_bytes <= FILE_MAX_BYTES
    assert SourceFileLimits().max_bytes <= TABLE_MAX_BYTES
