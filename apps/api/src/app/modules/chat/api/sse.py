"""Сериализация хода в SSE (docs/contracts.md §2) и сохранение ответа при обрыве стрима."""

from collections.abc import AsyncIterator
from contextlib import aclosing

import anyio
from fastapi.sse import format_sse_event

from app.modules.chat.application.turns import TurnStream


async def sse_stream(turn: TurnStream) -> AsyncIterator[bytes]:
    """`event: <type>` / `data: <Event JSON>` для каждого события хода.

    Обрыв соединения клиентом: Starlette отменяет стрим, `close` останавливает агента
    и записывает частичный ответ как `interrupted` (architecture.md §4). Под щитом: иначе
    отмена anyio прервала бы и запись. После `done` ответ уже записан — `close` только
    снимает ход с реестра.
    """
    try:
        async with aclosing(turn.events()) as events:
            async for event in events:
                yield format_sse_event(event=event.root.type, data_str=event.model_dump_json())
    finally:
        with anyio.CancelScope(shield=True):
            await turn.close()
