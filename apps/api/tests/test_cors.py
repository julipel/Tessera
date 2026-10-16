"""CORS для веб-чата: браузер шлёт `X-Widget-Key`, поэтому перед запросом идёт preflight."""

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.main import create_app
from app.settings import Settings

CHAT_ORIGIN = "http://chat.example"


def _preflight_headers(origin: str) -> dict[str, str]:
    return {
        "Origin": origin,
        "Access-Control-Request-Method": "POST",
        "Access-Control-Request-Headers": "content-type,x-widget-key",
    }


def _app(settings: Settings) -> FastAPI:
    return create_app(settings.model_copy(update={"cors_origins": [CHAT_ORIGIN]}))


async def test_preflight_from_allowed_origin(settings: Settings) -> None:
    async with AsyncClient(transport=ASGITransport(app=_app(settings)), base_url="http://t") as ac:
        resp = await ac.options("/v1/conversations", headers=_preflight_headers(CHAT_ORIGIN))

    assert resp.status_code == 200
    assert resp.headers["access-control-allow-origin"] == CHAT_ORIGIN
    assert "x-widget-key" in resp.headers["access-control-allow-headers"].lower()


async def test_preflight_from_unknown_origin_rejected(settings: Settings) -> None:
    async with AsyncClient(transport=ASGITransport(app=_app(settings)), base_url="http://t") as ac:
        resp = await ac.options(
            "/v1/conversations", headers=_preflight_headers("http://evil.example")
        )

    assert resp.status_code == 400
    assert "access-control-allow-origin" not in resp.headers


async def test_trace_id_exposed_to_browser(settings: Settings) -> None:
    async with AsyncClient(transport=ASGITransport(app=_app(settings)), base_url="http://t") as ac:
        resp = await ac.get("/health", headers={"Origin": CHAT_ORIGIN})

    assert resp.headers["access-control-allow-origin"] == CHAT_ORIGIN
    assert "x-trace-id" in resp.headers["access-control-expose-headers"].lower()
    assert resp.headers["x-trace-id"]


@pytest.mark.parametrize(
    ("method", "path"),
    [
        # Удаление файла источника (P8-03b), оценка ответа (P8-04a).
        ("DELETE", "/v1/admin/tenants/t/sources/s/files/a.md"),
        ("PUT", "/v1/admin/tenants/t/conversations/c/messages/m/rating"),
    ],
)
async def test_preflight_allows_admin_methods(settings: Settings, method: str, path: str) -> None:
    headers = {
        "Origin": CHAT_ORIGIN,
        "Access-Control-Request-Method": method,
        "Access-Control-Request-Headers": "authorization",
    }
    async with AsyncClient(transport=ASGITransport(app=_app(settings)), base_url="http://t") as ac:
        resp = await ac.options(path, headers=headers)

    assert resp.status_code == 200
    assert method in resp.headers["access-control-allow-methods"]


async def test_admin_preflight_allows_credentials(settings: Settings) -> None:
    """Сессия админки — cookie (ADR-0044): fetch с credentials требует allow-credentials."""
    async with AsyncClient(transport=ASGITransport(app=_app(settings)), base_url="http://t") as ac:
        resp = await ac.options(
            "/v1/admin/me",
            headers={"Origin": CHAT_ORIGIN, "Access-Control-Request-Method": "GET"},
        )

    assert resp.status_code == 200
    assert resp.headers["access-control-allow-origin"] == CHAT_ORIGIN
    assert resp.headers["access-control-allow-credentials"] == "true"


async def test_unknown_origin_cannot_read_admin_responses(settings: Settings) -> None:
    """Starlette ставит Allow-Credentials всем ответам, но без Allow-Origin браузер ответ
    чужому origin не отдаёт."""
    async with AsyncClient(transport=ASGITransport(app=_app(settings)), base_url="http://t") as ac:
        resp = await ac.get("/v1/admin/me", headers={"Origin": "http://evil.example"})

    assert "access-control-allow-origin" not in resp.headers


@pytest.mark.parametrize(
    "origin",
    [
        "*",
        "http://chat.example/",
        "chat.example",
        "http://chat.example/path",
        "HTTP://Chat.example",
        "http://chat.example:abc",
        "http://user@chat.example",
        "http://chat.example?",
    ],
)
def test_cors_origins_must_be_exact_origins(origin: str) -> None:
    """«*» с credentials — любой origin читает ответы админки; путь и слэш не совпадут с Origin."""
    with pytest.raises(ValueError, match="CORS_ORIGINS"):
        Settings(_env_file=None, cors_origins=[origin])


def test_cors_origins_accept_exact_origins() -> None:
    origins = ["https://chat.example", "https://chat.example:8443", "http://127.0.0.2:3001"]

    assert Settings(_env_file=None, cors_origins=origins).cors_origins == origins
