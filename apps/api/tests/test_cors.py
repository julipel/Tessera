"""CORS для веб-чата: браузер шлёт `X-Widget-Key`, поэтому перед запросом идёт preflight."""

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
