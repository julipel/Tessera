"""Результат инструмента (contracts.md §4)."""

from dataclasses import dataclass
from typing import Any, Literal

type ToolErrorCode = Literal[
    "validation_error", "not_found", "upstream_error", "timeout", "forbidden"
]


@dataclass(frozen=True, slots=True)
class ToolError:
    """`message` — понятное модели описание: по нему она может исправиться."""

    code: ToolErrorCode
    message: str
    retryable: bool


@dataclass(frozen=True, slots=True)
class ToolResult:
    """`content` — компактно для модели, `components` — UI-компоненты (contracts.md §3) для чата,
    `state_patch` — изменения DialogState, `suggestions` — быстрые ответы под сообщением
    (подписи, которые клиент отправит как текст пользователя)."""

    content: str | dict[str, Any] = ""
    components: tuple[dict[str, Any], ...] = ()
    state_patch: dict[str, Any] | None = None
    suggestions: tuple[str, ...] = ()
    error: ToolError | None = None

    @property
    def ok(self) -> bool:
        return self.error is None
