"""Лимит размера тела запроса до его чтения приложением (REVIEW.md: C-3).

FastAPI читает и разбирает JSON-тело до зависимостей эндпоинта: проверка размера в
зависимости (`limit_turn_body`) срабатывает, когда тело уже в памяти. Анонимный POST на
сотни мегабайт (chunked, без `Content-Length`) так и читался целиком. Middleware отвечает
413 по `Content-Length` сразу, а без него — как только прочитанное превысит лимит.
"""

import re
from collections.abc import Sequence

import structlog
from fastapi import status
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.modules.shared.api.errors import error_response

logger = structlog.get_logger(__name__)

# Правило: путь (regex, от начала) → лимит в байтах; None — без общего лимита (у эндпоинта
# своё потоковое чтение с лимитом).
type BodyLimitRule = tuple[str, int | None]


class BodySizeLimitMiddleware:
    """Первое подходящее правило задаёт лимит, иначе — `default_bytes`."""

    def __init__(
        self, app: ASGIApp, *, default_bytes: int, rules: Sequence[BodyLimitRule] = ()
    ) -> None:
        self.app = app
        self.default_bytes = default_bytes
        self.rules = [(re.compile(pattern), limit) for pattern, limit in rules]

    def _limit(self, path: str) -> int | None:
        for pattern, limit in self.rules:
            if pattern.match(path):
                return limit
        return self.default_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        limit = self._limit(scope["path"])
        if limit is None:
            await self.app(scope, receive, send)
            return
        length = dict(scope["headers"]).get(b"content-length", b"")
        if length.isdigit() and int(length) > limit:
            await _too_large(scope, receive, send, limit)
            return

        received = 0
        started = False
        rejected = False

        async def send_tracking(message: Message) -> None:
            nonlocal started
            if rejected:  # 413 уже отправлен — ответ приложения не нужен
                return
            if message["type"] == "http.response.start":
                started = True
            await send(message)

        async def receive_limited() -> Message:
            nonlocal received, rejected
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > limit and not started:
                    rejected = True
                    await _too_large(scope, receive, send, limit)
                    # Приложение больше не читает: для него клиент отключился.
                    return {"type": "http.disconnect"}
            return message

        try:
            await self.app(scope, receive_limited, send_tracking)
        except Exception:
            if not rejected:
                raise


async def _too_large(scope: Scope, receive: Receive, send: Send, limit: int) -> None:
    logger.warning("request_body_too_large", path=scope["path"], limit_bytes=limit)
    response = error_response(
        status.HTTP_413_CONTENT_TOO_LARGE, "invalid_input", f"тело запроса больше {limit} байт"
    )
    await response(scope, receive, send)
