"""Базовый репозиторий не выпускает данные за пределы тенанта."""

from collections.abc import AsyncIterator
from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncSession
from sqlalchemy.orm import Mapped

from app.modules.shared.public import (
    NotFoundError,
    TenantId,
    TenantMismatchError,
    TenantRepository,
    TenantScopedBase,
)


class Widget(TenantScopedBase):
    """Тестовая модель: таблица создаётся внутри транзакции теста, в миграциях её нет."""

    __tablename__ = "test_widgets"

    name: Mapped[str]


class WidgetRepository(TenantRepository[Widget]):
    model = Widget


TENANT_A = TenantId(uuid4())
TENANT_B = TenantId(uuid4())


@pytest.fixture
async def repo(
    db_connection: AsyncConnection, db_session: AsyncSession
) -> AsyncIterator[WidgetRepository]:
    await db_connection.run_sync(lambda c: Widget.metadata.tables[Widget.__tablename__].create(c))
    yield WidgetRepository(db_session)


async def _seed(repo: WidgetRepository) -> tuple[Widget, Widget]:
    a = await repo.add(TENANT_A, Widget(tenant_id=TENANT_A, name="a"))
    b = await repo.add(TENANT_B, Widget(tenant_id=TENANT_B, name="b"))
    return a, b


async def test_get_sees_only_own_tenant(repo: WidgetRepository) -> None:
    a, b = await _seed(repo)

    assert await repo.get(TENANT_A, a.id) is a
    assert await repo.get(TENANT_A, b.id) is None
    with pytest.raises(NotFoundError):
        await repo.get_or_raise(TENANT_B, a.id)


async def test_list_returns_only_own_tenant(repo: WidgetRepository) -> None:
    a, b = await _seed(repo)

    assert [w.id for w in await repo.list(TENANT_A)] == [a.id]
    assert [w.id for w in await repo.list(TENANT_B)] == [b.id]
    assert await repo.list(TENANT_A, Widget.name == "b") == []


async def test_delete_does_not_touch_other_tenant(repo: WidgetRepository) -> None:
    a, _ = await _seed(repo)

    assert await repo.delete(TENANT_B, a.id) is False
    assert await repo.get(TENANT_A, a.id) is a
    assert await repo.delete(TENANT_A, a.id) is True
    assert await repo.get(TENANT_A, a.id) is None


async def test_add_rejects_foreign_tenant_id(repo: WidgetRepository) -> None:
    with pytest.raises(TenantMismatchError):
        await repo.add(TENANT_A, Widget(tenant_id=TENANT_B, name="x"))


async def test_commit_in_session_is_rolled_back_after_test(
    repo: WidgetRepository, db_session: AsyncSession
) -> None:
    await _seed(repo)
    await db_session.commit()  # SAVEPOINT, внешняя транзакция откатится в фикстуре

    rows = (await db_session.execute(select(Widget))).scalars().all()
    assert len(rows) == 2


async def test_data_does_not_leak_between_tests(db_connection: AsyncConnection) -> None:
    # Таблица создаётся только в транзакции теста — после отката её нет.
    exists = await db_connection.run_sync(lambda c: c.dialect.has_table(c, Widget.__tablename__))
    assert exists is False
