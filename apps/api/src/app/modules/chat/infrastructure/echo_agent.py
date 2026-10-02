"""Заглушка агента до P2-08: повторяет ввод пользователя, отдавая текст кусками по словам."""

import asyncio
import re
from collections.abc import AsyncIterator
from typing import Any

from app.modules.chat.domain.entities import AgentTextDelta, TurnRequest


def echo_text(user_input: dict[str, Any]) -> str:
    match user_input.get("type"):
        case "text":
            return f"Вы написали: {user_input['text']}"
        case "action":
            return f"Нажата кнопка: {user_input['action_id']}"
        case "form_submit":
            return f"Отправлена форма: {user_input['form_id']}"
        case _:
            return "Получен ввод неизвестного типа"


class EchoAgent:
    """`chunk_delay` — пауза перед каждым куском, чтобы стриминг был виден в чате."""

    def __init__(self, chunk_delay: float = 0.0) -> None:
        self.chunk_delay = chunk_delay

    async def run_turn(self, request: TurnRequest) -> AsyncIterator[AgentTextDelta]:
        # Слово вместе с хвостовыми пробелами: склейка кусков даёт исходный текст.
        for chunk in re.findall(r"\S+\s*", echo_text(request.input)):
            if self.chunk_delay:
                await asyncio.sleep(self.chunk_delay)
            yield AgentTextDelta(chunk)
