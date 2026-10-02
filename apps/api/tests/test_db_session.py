"""Зависимость DbSession: сессия на запрос из фабрики приложения."""

from fastapi import FastAPI
from httpx import AsyncClient
from sqlalchemy import text

from app.modules.shared.public import DbSession


async def test_db_session_dependency(app: FastAPI, client: AsyncClient, db_url: str) -> None:
    @app.get("/_db_ping")
    async def ping(session: DbSession) -> int | None:
        result: int | None = await session.scalar(text("SELECT 1"))
        return result

    response = await client.get("/_db_ping")

    assert response.status_code == 200
    assert response.json() == 1
    await app.state.engine.dispose()
