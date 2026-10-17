"""Репозитории tenants: изоляция тенантов (ADR-0006), версии AgentConfig, единственный active."""

import asyncio
from collections.abc import AsyncIterator
from uuid import uuid4

import pytest
from sqlalchemy import delete
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, create_async_engine

from app.modules.shared.public import TenantId
from app.modules.tenants.public import (
    AgentConfigNotFoundError,
    AgentConfigRecord,
    AgentConfigRepository,
    AgentConfigStatus,
    SqlTenantDirectory,
    TenantRecord,
    WidgetKeyRepository,
)

CONFIG = {"assistant": {"name": "A"}}


@pytest.fixture
async def tenants(db_session: AsyncSession) -> tuple[TenantId, TenantId]:
    directory = SqlTenantDirectory(db_session)
    a = await directory.create("tenant-a", "A")
    b = await directory.create("tenant-b", "B")
    return a.id, b.id


@pytest.fixture
def configs(db_session: AsyncSession) -> AgentConfigRepository:
    return AgentConfigRepository(db_session)


async def test_tenant_directory_finds_by_slug(db_session: AsyncSession) -> None:
    directory = SqlTenantDirectory(db_session)
    created = await directory.create("shop", "Shop")
    await directory.rename(created.id, "Shop 2")

    found = await directory.get_by_slug("shop")
    assert found is not None
    assert (found.id, found.name) == (created.id, "Shop 2")
    assert await directory.get_by_slug("missing") is None


async def test_versions_are_numbered_per_tenant(
    configs: AgentConfigRepository, tenants: tuple[TenantId, TenantId]
) -> None:
    a, b = tenants
    assert (await configs.create_draft(a, CONFIG)).version == 1
    assert (await configs.create_draft(a, CONFIG)).version == 2
    assert (await configs.create_draft(b, CONFIG)).version == 1
    assert [v.version for v in await configs.list_versions(a)] == [1, 2]


async def test_activate_archives_previous_active(
    configs: AgentConfigRepository, tenants: tuple[TenantId, TenantId]
) -> None:
    a, _ = tenants
    v1 = await configs.create_draft(a, CONFIG)
    first = await configs.activate(a, v1.id)
    v2 = await configs.create_draft(a, {"assistant": {"name": "B"}})
    activated = await configs.activate(a, v2.id)

    assert first.previous_version is None  # активной версии ещё не было
    assert activated.config.status == AgentConfigStatus.ACTIVE
    assert activated.previous_version == 1
    active = await configs.get_active(a)
    assert active is not None
    assert active.id == v2.id
    statuses = {v.version: v.status for v in await configs.list_versions(a)}
    assert statuses == {1: AgentConfigStatus.ARCHIVED, 2: AgentConfigStatus.ACTIVE}


async def test_activate_is_idempotent(
    configs: AgentConfigRepository, tenants: tuple[TenantId, TenantId]
) -> None:
    a, _ = tenants
    v1 = await configs.create_draft(a, CONFIG)
    await configs.activate(a, v1.id)
    again = await configs.activate(a, v1.id)
    assert again.config.status == AgentConfigStatus.ACTIVE
    assert again.previous_version == 1


async def test_database_allows_single_active_config(
    db_session: AsyncSession, tenants: tuple[TenantId, TenantId]
) -> None:
    a, _ = tenants
    for version in (1, 2):
        db_session.add(
            AgentConfigRecord(
                tenant_id=a, version=version, status=AgentConfigStatus.ACTIVE, config=CONFIG
            )
        )
    with pytest.raises(IntegrityError):
        async with db_session.begin_nested():
            await db_session.flush()


async def test_agent_config_is_isolated_between_tenants(
    configs: AgentConfigRepository, tenants: tuple[TenantId, TenantId]
) -> None:
    a, b = tenants
    v1 = await configs.create_draft(a, CONFIG)
    await configs.activate(a, v1.id)

    assert await configs.get(b, v1.id) is None
    assert await configs.get_active(b) is None
    assert await configs.list_versions(b) == []
    with pytest.raises(AgentConfigNotFoundError):
        await configs.activate(b, v1.id)
    assert await configs.delete(b, v1.id) is False
    assert await configs.get(a, v1.id) is not None


async def test_widget_keys_are_isolated_between_tenants(
    db_session: AsyncSession, tenants: tuple[TenantId, TenantId]
) -> None:
    a, b = tenants
    keys = WidgetKeyRepository(db_session)
    await keys.add_key(a, "0" * 64, ["https://shop.example"])

    assert await keys.has_any(a) is True
    assert await keys.has_any(b) is False
    assert await keys.has_key(a, "0" * 64) is True
    assert await keys.has_key(a, "1" * 64) is False
    assert await keys.has_key(b, "0" * 64) is False  # хэш чужого ключа не находится
    assert await keys.list(b) == []
    [key] = await keys.list(a)
    assert key.allowed_origins == ["https://shop.example"]
    assert await keys.delete(b, key.id) is False
    assert await keys.get(b, uuid4()) is None


async def test_version_numbers_by_id_within_tenant(
    configs: AgentConfigRepository, tenants: tuple[TenantId, TenantId]
) -> None:
    a, b = tenants
    v1, v2 = await configs.create_draft(a, CONFIG), await configs.create_draft(a, CONFIG)
    foreign = await configs.create_draft(b, CONFIG)

    assert await configs.version_numbers(a, {v1.id, v2.id}) == {v1.id: 1, v2.id: 2}
    # Версия чужого тенанта и неизвестный id не находятся; пустой набор — без запроса.
    assert await configs.version_numbers(a, {foreign.id, uuid4()}) == {}
    assert await configs.version_numbers(b, {foreign.id, v1.id}) == {foreign.id: 1}
    assert await configs.version_numbers(a, set()) == {}


async def test_get_version_returns_archived_and_respects_tenant(
    configs: AgentConfigRepository, tenants: tuple[TenantId, TenantId]
) -> None:
    a, b = tenants
    v1 = await configs.create_draft(a, CONFIG)
    await configs.activate(a, v1.id)
    v2 = await configs.create_draft(a, CONFIG)
    await configs.activate(a, v2.id)

    archived = await configs.get_version(a, v1.id)

    assert archived is not None
    assert (archived.version, archived.status) == (1, AgentConfigStatus.ARCHIVED)
    assert await configs.get_version(b, v1.id) is None
    assert await configs.get_version(a, uuid4()) is None


# --- Параллельные операции с версиями (T-2, T-3): две транзакции с настоящим commit ---


@pytest.fixture
async def engine(db_url: str) -> AsyncIterator[AsyncEngine]:
    engine = create_async_engine(db_url)
    yield engine
    await engine.dispose()


@pytest.fixture
async def committed_tenant(engine: AsyncEngine) -> AsyncIterator[TenantId]:
    """Тенант вне транзакции теста, его видят две сессии; удаляется вместе с версиями."""
    async with AsyncSession(engine) as session:
        tenant = await SqlTenantDirectory(session).create(f"race-{uuid4().hex[:8]}", "Race")
        await session.commit()
    yield tenant.id
    async with AsyncSession(engine) as session:
        await session.execute(delete(TenantRecord).where(TenantRecord.id == tenant.id))
        await session.commit()


async def _blocked(task: asyncio.Task[object]) -> bool:
    await asyncio.sleep(0.2)
    return not task.done()


async def test_parallel_drafts_wait_for_each_other(
    engine: AsyncEngine, committed_tenant: TenantId
) -> None:
    async with AsyncSession(engine) as first, AsyncSession(engine) as second:
        draft_1 = await AgentConfigRepository(first).create_draft(committed_tenant, CONFIG)

        async def save_second() -> int:
            draft = await AgentConfigRepository(second).create_draft(committed_tenant, CONFIG)
            await second.commit()
            return draft.version

        task = asyncio.create_task(save_second())
        assert await _blocked(task)  # ждёт блокировку тенанта, а не падает на уникальном индексе
        await first.commit()

        assert (draft_1.version, await task) == (1, 2)


async def test_parallel_activations_leave_single_active(
    engine: AsyncEngine, committed_tenant: TenantId
) -> None:
    async with AsyncSession(engine) as session:
        configs = AgentConfigRepository(session)
        v1 = await configs.create_draft(committed_tenant, CONFIG)
        await configs.activate(committed_tenant, v1.id)
        v2 = await configs.create_draft(committed_tenant, CONFIG)
        v3 = await configs.create_draft(committed_tenant, CONFIG)
        await session.commit()

    async with AsyncSession(engine) as first, AsyncSession(engine) as second:
        await AgentConfigRepository(first).activate(committed_tenant, v2.id)

        async def activate_second() -> None:
            await AgentConfigRepository(second).activate(committed_tenant, v3.id)
            await second.commit()

        task = asyncio.create_task(activate_second())
        assert await _blocked(task)
        await first.commit()
        await task

    async with AsyncSession(engine) as session:
        versions = await AgentConfigRepository(session).list_versions(committed_tenant)
    assert [(v.version, v.status) for v in versions] == [
        (1, AgentConfigStatus.ARCHIVED),
        (2, AgentConfigStatus.ARCHIVED),
        (3, AgentConfigStatus.ACTIVE),
    ]
