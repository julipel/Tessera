"""Сериализация хода в SSE (docs/contracts.md §2) и сохранение ответа при обрыве стрима."""

from collections.abc import AsyncIterator
from contextlib import aclosing

import anyio
from fastapi.sse import KEEPALIVE_COMMENT, EventSourceResponse, format_sse_event
from starlette.types import Receive, Scope, Send

from app.contracts import Event
from app.modules.chat.application.turns import TurnStream

# Пинг, если агент молчит столько секунд (как у SSE-эндпоинтов FastAPI): idle-таймауты
# прокси — Cloudflare Tunnel ~100 с, nginx 60 с по умолчанию (REVIEW.md: C-6).
PING_AFTER_S = 15.0


async def sse_stream(turn: TurnStream, ping_after_s: float = PING_AFTER_S) -> AsyncIterator[bytes]:
    """`event: <type>` / `data: <Event JSON>` для каждого события хода и `: ping`, пока
    агент молчит.

    Обрыв соединения клиентом: Starlette отменяет стрим, `close` останавливает агента
    и записывает частичный ответ как `interrupted` (architecture.md §4). Под щитом: иначе
    отмена anyio прервала бы и запись. После `done` ответ уже записан — `close` только
    снимает ход с реестра.
    """
    try:
        async with aclosing(turn.items(ping_after_s)) as items:
            async for item in items:
                if isinstance(item, Event):
                    yield format_sse_event(event=item.root.type, data_str=item.model_dump_json())
                else:
                    yield KEEPALIVE_COMMENT
    finally:
        with anyio.CancelScope(shield=True):
            await turn.close()


class TurnResponse(EventSourceResponse):
    """SSE-ответ хода, который закрывает ход, даже если стрим так и не начался.

    `finally` в `sse_stream` выполняется, только если генератор запущен; если отправка
    заголовков упала (клиент уже отключился), Starlette генератор не закрывает, и диалог
    остался бы занятым до истечения срока (ADR-0041). `close` повторно ничего не меняет.

    Заголовки — как у SSE-эндпоинтов FastAPI: без кэша и без буферизации в nginx."""

    def __init__(self, turn: TurnStream, ping_after_s: float = PING_AFTER_S) -> None:
        super().__init__(sse_stream(turn, ping_after_s))
        self.headers["Cache-Control"] = "no-cache"
        self.headers["X-Accel-Buffering"] = "no"
        self._turn = turn

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        try:
            await super().__call__(scope, receive, send)
        finally:
            with anyio.CancelScope(shield=True):
                await self._turn.close()
