import asyncio
import os
from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import exc, make_url, text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncSession, create_async_engine

from app.main import create_app
from app.settings import Settings

ALEMBIC_INI = Path(__file__).resolve().parents[1] / "alembic.ini"


def database_url_for_tests() -> str:
    """Отдельная БД для тестов: TEST_DATABASE_URL или DATABASE_URL с именем `app_test`."""
    if url := os.environ.get("TEST_DATABASE_URL"):
        return url
    base = Settings(_env_file=None).database_url
    return make_url(base).set(database="app_test").render_as_string(hide_password=False)


def alembic_config(url: str) -> Config:
    cfg = Config(ALEMBIC_INI)
    cfg.set_main_option("sqlalchemy.url", url)
    cfg.attributes["configure_logger"] = False
    return cfg


@pytest.fixture
def settings() -> Settings:
    return Settings(_env_file=None, app_env="test", database_url=database_url_for_tests())


@pytest.fixture
def app(settings: Settings) -> FastAPI:
    return create_app(settings)


@pytest.fixture
async def client(app: FastAPI) -> AsyncIterator[AsyncClient]:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        yield ac


# --- БД: тестовая база с миграциями на сессию, транзакция с откатом на тест ---


async def _ensure_database(url: str) -> None:
    target = make_url(url)
    admin = create_async_engine(target.set(database="postgres"), isolation_level="AUTOCOMMIT")
    try:
        async with admin.connect() as conn:
            exists = await conn.scalar(
                text("SELECT 1 FROM pg_database WHERE datname = :name"),
                {"name": target.database},
            )
            if not exists:
                await conn.execute(text(f'CREATE DATABASE "{target.database}"'))
    finally:
        await admin.dispose()


@pytest.fixture
def alembic_cfg(db_url: str) -> Config:
    return alembic_config(db_url)


@pytest.fixture(scope="session")
def db_url() -> Iterator[str]:
    url = database_url_for_tests()
    try:
        asyncio.run(_ensure_database(url))
    except (OSError, exc.SQLAlchemyError) as e:
        pytest.fail(f"Postgres недоступен ({e}). Запусти `make up`.", pytrace=False)
    command.upgrade(alembic_config(url), "head")
    yield url


@pytest.fixture
async def db_connection(db_url: str) -> AsyncIterator[AsyncConnection]:
    """Соединение во внешней транзакции, которая откатывается после теста."""
    engine = create_async_engine(db_url)
    async with engine.connect() as conn:
        trans = await conn.begin()
        try:
            yield conn
        finally:
            await trans.rollback()
    await engine.dispose()


@pytest.fixture
async def db_session(db_connection: AsyncConnection) -> AsyncIterator[AsyncSession]:
    # create_savepoint: commit/rollback сессии идут через SAVEPOINT, внешняя транзакция цела.
    async with AsyncSession(
        bind=db_connection, join_transaction_mode="create_savepoint", expire_on_commit=False
    ) as session:
        yield session
