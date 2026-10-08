"""Источники знаний в админке (P8-03a, ADR-0037): список, создание, синхронизация, история."""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.contracts import SourceDetail, SourceList, SourceSyncItem, SourceSyncList
from app.modules.knowledge.public import (
    DocumentItem,
    EntityItem,
    InvalidSourceDeclarationError,
    LocalSourceFileStore,
    SourceDeclaration,
    SourceKind,
    SourceRepository,
    SqlSyncStore,
    SyncStatus,
    seed_sources,
    validate_source_config,
)
from app.modules.shared.public import AdminRole, TenantId
from app.modules.tenants.public import SqlTenantDirectory

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
