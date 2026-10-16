"""Зависимость DbSession: сессия на запрос из фабрики приложения."""

import time
from collections.abc import AsyncIterator
from uuid import uuid4

import pytest
from fastapi import FastAPI, status
from fastapi.responses import StreamingResponse
from httpx import AsyncClient
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import create_async_engine

from app.modules.shared.public import ApiError, DbSession, StreamDbSession, create_engine
from app.modules.tenants.public import SqlTenantDirectory, TenantRecord


async def test_db_session_dependency(app: FastAPI, client: AsyncClient, db_url: str) -> None:
    @app.get("/_db_ping")
    async def ping(session: DbSession) -> int | None:
        result: int | None = await session.scalar(text("SELECT 1"))
        return result

    response = await client.get("/_db_ping")

    assert response.status_code == 200
    assert response.json() == 1
    await app.state.engine.dispose()


# --- поведение get_session (REVIEW.md: S-6) и таймаут запроса (S-4) ---
# Эти тесты коммитят в тестовую базу по-настоящему (без внешней транзакции): тенант со
# случайным slug удаляется в конце.


@pytest.fixture
async def slug(db_url: str) -> AsyncIterator[str]:
    value = f"s6-{uuid4().hex[:12]}"
    yield value
    engine = create_async_engine(db_url)
    async with engine.begin() as conn:
        await conn.execute(text("DELETE FROM tenants WHERE slug LIKE :s"), {"s": f"{value}%"})
    await engine.dispose()


async def _tenant_exists(db_url: str, slug: str) -> bool:
    engine = create_async_engine(db_url)
    async with engine.connect() as conn:
        found = await conn.scalar(text("SELECT count(*) FROM tenants WHERE slug = :s"), {"s": slug})
    await engine.dispose()
    return bool(found)


async def test_session_commits_on_success(
    app: FastAPI, client: AsyncClient, db_url: str, slug: str
) -> None:
    @app.post("/_s6")
    async def create(session: DbSession) -> dict[str, bool]:
        await SqlTenantDirectory(session).create(slug, slug)
        return {"ok": True}

    assert (await client.post("/_s6")).status_code == 200
    assert await _tenant_exists(db_url, slug)
    await app.state.engine.dispose()


async def test_session_rolls_back_on_api_error(
    app: FastAPI, client: AsyncClient, db_url: str, slug: str
) -> None:
    @app.post("/_s6")
    async def create(session: DbSession) -> None:
        await SqlTenantDirectory(session).create(slug, slug)
        raise ApiError(status.HTTP_404_NOT_FOUND, "not_found", "передумали")

    response = await client.post("/_s6")

    assert response.status_code == 404
    assert not await _tenant_exists(db_url, slug)
    await app.state.engine.dispose()


async def test_commit_failure_is_500_not_success(
    app: FastAPI, client: AsyncClient, db_url: str, slug: str
) -> None:
    """Ошибка commit (здесь — нарушение уникальности slug при записи) не превращается в 200:
    scope="function" коммитит до отправки ответа."""

    @app.post("/_s6")
    async def create(session: DbSession) -> dict[str, bool]:
        session.add_all([TenantRecord(slug=slug, name="a"), TenantRecord(slug=slug, name="b")])
        return {"ok": True}

    response = await client.post("/_s6")

    assert response.status_code == 500
    assert response.json()["error"]["code"] == "internal"
    assert not await _tenant_exists(db_url, slug)
    await app.state.engine.dispose()


async def test_stream_session_does_not_commit_when_stream_fails(
    app: FastAPI, client: AsyncClient, db_url: str, slug: str
) -> None:
    """StreamDbSession живёт до конца отправки ответа: сбой посреди стрима откатывает то, что
    стрим не закоммитил сам."""

    @app.get("/_s6_stream")
    async def stream(session: StreamDbSession) -> StreamingResponse:
        async def body() -> AsyncIterator[bytes]:
            yield b"start\n"
            await SqlTenantDirectory(session).create(slug, slug)
            raise RuntimeError("обрыв посреди стрима")

        return StreamingResponse(body())

    with pytest.raises(RuntimeError):
        await client.get("/_s6_stream")

    assert not await _tenant_exists(db_url, slug)
    await app.state.engine.dispose()


async def test_statement_timeout_cancels_hung_query(db_url: str) -> None:
    engine = create_engine(db_url, statement_timeout_s=0.3)
    started = time.perf_counter()

    with pytest.raises(DBAPIError, match="statement timeout"):
        async with engine.connect() as conn:
            await conn.execute(text("SELECT pg_sleep(5)"))

    assert time.perf_counter() - started < 3
    await engine.dispose()
