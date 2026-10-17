from collections.abc import Iterator

import pytest
from httpx import AsyncClient

from app.logs import TRACE_ID_HEADER, configure_logging
from app.main import create_app
from app.settings import Settings


async def test_health_ok(client: AsyncClient) -> None:
    response = await client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


async def test_trace_id_is_propagated(client: AsyncClient) -> None:
    response = await client.get("/health", headers={TRACE_ID_HEADER: "abc123"})

    assert response.headers[TRACE_ID_HEADER] == "abc123"


async def test_trace_id_is_generated_when_missing(client: AsyncClient) -> None:
    first = await client.get("/health")
    second = await client.get("/health")

    assert first.headers[TRACE_ID_HEADER]
    assert first.headers[TRACE_ID_HEADER] != second.headers[TRACE_ID_HEADER]


@pytest.fixture
def restore_logging() -> Iterator[None]:
    """create_app настраивает логи глобально — вернуть тестовую конфигурацию."""
    try:
        yield
    finally:
        configure_logging(Settings(_env_file=None, app_env="test"))


@pytest.mark.usefixtures("restore_logging")
def test_openapi_and_docs_only_locally() -> None:
    """В проде схему и Swagger закрывает Caddy, туннель demo-public — API_DOCS=false (R-2)."""
    production = create_app(
        Settings(
            _env_file=None,
            app_env="production",
            database_url="postgresql+asyncpg://app:s3cret@postgres:5432/app",
            redis_url="redis://:s3cret@redis:6379/0",
            qdrant_api_key="s3cret",
            cors_origins=["https://chat.example.com"],
        )
    )
    local = create_app(Settings(_env_file=None, app_env="local"))
    tunnel = create_app(Settings(_env_file=None, app_env="local", api_docs=False))

    for app in (production, tunnel):
        assert (app.openapi_url, app.docs_url, app.redoc_url) == (None, None, None)
    assert (local.openapi_url, local.docs_url) == ("/openapi.json", "/docs")
