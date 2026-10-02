"""Сериализация хода в SSE (docs/contracts.md §2) и сохранение ответа при обрыве стрима."""

from collections.abc import AsyncIterator
from contextlib import aclosing

import anyio
from fastapi.sse import format_sse_event

from app.modules.chat.application.turns import TurnStream
from app.modules.chat.domain.entities import MessageStatus


async def sse_stream(turn: TurnStream) -> AsyncIterator[bytes]:
    """`event: <type>` / `data: <Event JSON>` для каждого события хода.

    Обрыв соединения клиентом: Starlette отменяет стрим, частичный ответ записывается
    как `interrupted` (architecture.md §4). Запись — под щитом: иначе отмена anyio
    прервала бы и её. После `done` ответ уже записан, и `save` ничего не делает.
    """
    try:
        async with aclosing(turn.events()) as events:
            async for event in events:
                yield format_sse_event(event=event.root.type, data_str=event.model_dump_json())
    finally:
        with anyio.CancelScope(shield=True):
            await turn.save(MessageStatus.INTERRUPTED)
