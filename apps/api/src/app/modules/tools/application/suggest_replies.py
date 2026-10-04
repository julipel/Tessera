"""Встроенный инструмент `suggest_replies` (contracts.md §2, §4, ADR-0020): быстрые ответы
под сообщением ассистента. Модель передаёт короткие реплики от лица пользователя, чат
показывает их кнопками; нажатие отправляет реплику как обычный текст пользователя.
"""

from collections.abc import Mapping
from typing import Any

from app.modules.tools.domain.definition import ToolContext, ToolDefinition
from app.modules.tools.domain.result import ToolResult

SUGGEST_REPLIES = "suggest_replies"
OPTIONS_MAX = 4
OPTION_MAX_LENGTH = 40

_DESCRIPTION = (
    "Покажи пользователю кнопки быстрых ответов под своим сообщением: 2-4 короткие реплики "
    "от лица пользователя — типичные ответы на твой уточняющий вопрос или очевидные "
    "следующие шаги. Вызывай в том же ответе, что и текст сообщения, и не перечисляй эти "
    "варианты в тексте. Без цен, ссылок и названий, которых нет в данных."
)


def suggest_replies_tool() -> ToolDefinition:
    return ToolDefinition(
        name=SUGGEST_REPLIES,
        description=_DESCRIPTION,
        parameters={
            "type": "object",
            "properties": {
                "options": {
                    "type": "array",
                    "items": {"type": "string", "minLength": 1, "maxLength": OPTION_MAX_LENGTH},
                    "minItems": 1,
                    "maxItems": OPTIONS_MAX,
                    "uniqueItems": True,
                    "description": "Реплики пользователя в порядке показа.",
                },
            },
            "required": ["options"],
            "additionalProperties": False,
        },
        handler=_suggest_replies,
        timeout_s=1,
    )


async def _suggest_replies(arguments: Mapping[str, Any], ctx: ToolContext, /) -> ToolResult:
    options = [option.strip() for option in arguments["options"] if option.strip()]
    if not options:
        return ToolResult(content="Нет вариантов ответа — кнопки не показаны.")
    return ToolResult(
        content="Кнопки показаны пользователю; ответ ниже не дописывай.",
        suggestions=tuple(dict.fromkeys(options)),
    )
