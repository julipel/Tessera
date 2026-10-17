"""Запрос к ASGI-приложению без httpx: транспорт httpx требует завершённого ответа, а здесь
проверяется, что сервер получает при ошибке посреди стрима."""

from starlette.types import ASGIApp, Message


async def raw_get(
    app: ASGIApp, path: str, headers: list[tuple[bytes, bytes]] | None = None
) -> list[Message]:
    """GET `path`; возвращает отправленные приложением сообщения. Исключение приложения
    пробрасывается."""
    scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.4"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "root_path": "",
        "headers": [(b"host", b"test"), *(headers or [])],
        "client": ("127.0.0.1", 50000),
        "server": ("test", 80),
        "state": {},
    }
    sent: list[Message] = []

    async def receive() -> Message:
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message: Message) -> None:
        sent.append(message)

    await app(scope, receive, send)
    return sent


def body_of(messages: list[Message]) -> bytes:
    return b"".join(m.get("body", b"") for m in messages if m["type"] == "http.response.body")


def completed(messages: list[Message]) -> bool:
    """Ответ дописан: последнее сообщение тела без `more_body`."""
    bodies = [m for m in messages if m["type"] == "http.response.body"]
    return bool(bodies) and not bodies[-1].get("more_body", False)
