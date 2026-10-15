"""Лимит размера тела до чтения приложением (REVIEW.md: C-3)."""

from collections.abc import AsyncIterator

from fastapi import FastAPI, Request
from httpx import ASGITransport, AsyncClient

from app.contracts import HttpError
from app.modules.shared.public import BodySizeLimitMiddleware


def _app() -> tuple[FastAPI, list[int]]:
    app = FastAPI()
    read: list[int] = []

    @app.post("/small")
    async def small(request: Request) -> dict[str, int]:
        read.append(len(await request.body()))
        return {"size": read[-1]}

    @app.post("/files/upload")
    async def upload(request: Request) -> dict[str, int]:
        read.append(len(await request.body()))
        return {"size": read[-1]}

    app.add_middleware(
        BodySizeLimitMiddleware, default_bytes=100, rules=[(r"/files/", None), (r"/small", 10)]
    )
    return app, read


def _client(app: FastAPI) -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


def _assert_too_large(status: int, body: bytes) -> None:
    assert status == 413
    error = HttpError.model_validate_json(body).error
    assert error.code == "invalid_input"
    assert "больше 10 байт" in error.message


async def test_content_length_over_limit_is_rejected_before_reading() -> None:
    app, read = _app()
    async with _client(app) as client:
        response = await client.post("/small", content=b"x" * 11)

    _assert_too_large(response.status_code, response.content)
    assert read == []  # эндпоинт не вызывался


async def test_chunked_body_over_limit_is_rejected_while_reading() -> None:
    app, read = _app()

    async def chunks() -> AsyncIterator[bytes]:
        for _ in range(5):  # без Content-Length: 5 кусков по 4 байта
            yield b"xxxx"

    async with _client(app) as client:
        response = await client.post("/small", content=chunks())

    _assert_too_large(response.status_code, response.content)
    assert read == []


async def test_body_within_limit_passes() -> None:
    app, read = _app()
    async with _client(app) as client:
        response = await client.post("/small", content=b"x" * 10)

    assert (response.status_code, response.json()) == (200, {"size": 10})
    assert read == [10]


async def test_path_without_limit_is_not_limited() -> None:
    app, read = _app()
    async with _client(app) as client:
        response = await client.post("/files/upload", content=b"x" * 1000)

    assert response.status_code == 200
    assert read == [1000]


async def test_chat_body_limit_in_app(db_client: AsyncClient) -> None:
    """В приложении чат ограничен 64 КБ до проверки ключа виджета: ключа нет — всё равно 413."""
    response = await db_client.post(
        f"/v1/conversations/{'0' * 8}-0000-0000-0000-{'0' * 12}/messages",
        content=b"x" * (64 * 1024 + 1),
        headers={"Content-Type": "application/json"},
    )

    assert response.status_code == 413
    assert HttpError.model_validate(response.json()).error.code == "invalid_input"
