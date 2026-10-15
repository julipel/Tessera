"""Соединения пула на запрос (P7-05): ход не держит два соединения одновременно.

Нагрузочный тест показал взаимную блокировку: проверка ключа виджета держала соединение
(`DbSession`) до выхода из эндпоинта, а ход в это время брал второе (`StreamDbSession`).
При занятом пуле все запросы ждали второе соединение до таймаута пула."""

import uuid
from collections.abc import AsyncIterator

import pytest
from fastapi import FastAPI
from httpx import AsyncClient
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from app.main import create_app
from app.modules.shared.public import (
    StreamDbSession,
    TenantId,
    create_engine,
    create_session_factory,
)
from app.modules.tenants.domain.entities import WidgetAccess
from app.modules.tenants.infrastructure.repositories import SqlWidgetKeyResolver
from app.modules.tenants.public import WidgetTenant
from app.settings import Settings


@pytest.fixture
async def single_connection_app(
    app: FastAPI, db_url: str, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[FastAPI]:
    """Пул из одного соединения; ключ виджета «находится» запросом к БД, как настоящий."""

    async def resolve(self: SqlWidgetKeyResolver, key_hash: str) -> WidgetAccess:
        await self.session.execute(text("SELECT 1"))
        return WidgetAccess(tenant_id=TenantId(uuid.uuid4()), allowed_origins=())

    monkeypatch.setattr(SqlWidgetKeyResolver, "resolve", resolve)
    engine = create_engine(db_url, pool_size=1, max_overflow=0, pool_timeout=2)
    app.state.session_factory = create_session_factory(engine)

    @app.post("/_stream_turn")
    async def stream_turn(tenant_id: WidgetTenant, session: StreamDbSession) -> int | None:
        result: int | None = await session.scalar(text("SELECT 1"))
        return result

    yield app
    await engine.dispose()
    await app.state.engine.dispose()


async def test_widget_auth_releases_connection_before_turn(
    single_connection_app: FastAPI, client: AsyncClient
) -> None:
    response = await client.post("/_stream_turn", headers={"X-Widget-Key": "wk_test"})

    assert response.status_code == 200
    assert response.json() == 1


async def test_app_engine_pool_from_settings(settings: Settings) -> None:
    app = create_app(settings.model_copy(update={"db_pool_size": 20, "db_max_overflow": 5}))
    pool = app.state.engine.pool
    assert (pool.size(), pool._max_overflow) == (20, 5)
    await app.state.engine.dispose()


async def test_db_errors_do_not_contain_query_parameters(db_url: str) -> None:
    """Текст ошибки уходит в лог (`logger.exception`): значения запроса — контакты из
    заявок — в нём не показываются (REVIEW.md: L-2)."""
    engine = create_engine(db_url)
    try:
        async with engine.connect() as conn:
            with pytest.raises(DBAPIError) as raised:
                # \u0000 Postgres не принимает в JSONB — ошибка БД с параметром-телефоном.
                await conn.execute(
                    text("SELECT CAST(:value AS jsonb)"),
                    {"value": '{"phone": "+79991234567\\u0000"}'},
                )
    finally:
        await engine.dispose()

    assert "79991234567" not in str(raised.value)
    assert "hide_parameters" in str(raised.value)
