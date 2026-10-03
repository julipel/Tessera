"""Порт LLM-провайдера (architecture.md §5): запрос, чанки стрима, финальный ответ.

Типы не зависят от SDK провайдеров; адаптеры (OpenAI, Anthropic) переводят их в формат API.
"""

from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Protocol

from app.modules.shared.kernel import DomainError


@dataclass(frozen=True, slots=True)
class ToolCall:
    """Вызов инструмента, запрошенный моделью.

    `raw_arguments` — аргументы как их прислала модель (строка JSON). `arguments` — результат
    разбора или None, если это не JSON-объект: цикл не падает, а возвращает модели ошибку.
    """

    id: str
    name: str
    arguments: dict[str, Any] | None
    raw_arguments: str


@dataclass(frozen=True, slots=True)
class UserMessage:
    text: str


type ProviderItem = dict[str, Any]


@dataclass(frozen=True, slots=True)
class AssistantMessage:
    """`provider_items` — непрозрачные данные провайдера, которые он просит вернуть во входе
    следующего шага (ADR-0010; например, reasoning items Responses API). Живут в пределах хода,
    в БД не пишутся. Адаптер, который их не понимает, игнорирует поле."""

    text: str
    tool_calls: tuple[ToolCall, ...] = ()
    provider_items: tuple[ProviderItem, ...] = ()


@dataclass(frozen=True, slots=True)
class ToolResultMessage:
    """Результат инструмента для модели; `is_error` — ошибка исполнения или валидации."""

    tool_call_id: str
    content: str
    is_error: bool = False


type LLMMessage = UserMessage | AssistantMessage | ToolResultMessage


@dataclass(frozen=True, slots=True)
class ToolSchema:
    """Описание инструмента для модели; `parameters` — JSON Schema аргументов."""

    name: str
    description: str
    parameters: dict[str, Any]


@dataclass(frozen=True, slots=True)
class LLMRequest:
    """`system` — отдельно от сообщений, как в API Anthropic; адаптер OpenAI сам
    добавит system-сообщение в начало."""

    model: str
    system: str
    messages: tuple[LLMMessage, ...]
    tools: tuple[ToolSchema, ...] = ()
    temperature: float | None = None
    max_output_tokens: int | None = None


class StopReason(StrEnum):
    END_TURN = "end_turn"
    TOOL_CALLS = "tool_calls"
    MAX_TOKENS = "max_tokens"


@dataclass(frozen=True, slots=True)
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0


@dataclass(frozen=True, slots=True)
class LLMResponse:
    """Полный ответ шага модели; `text` — склейка всех TextDelta. `provider_items` цикл
    возвращает в следующий шаг хода без изменений (см. AssistantMessage)."""

    text: str
    tool_calls: tuple[ToolCall, ...] = ()
    stop_reason: StopReason = StopReason.END_TURN
    usage: Usage = field(default_factory=Usage)
    provider_items: tuple[ProviderItem, ...] = ()

    def as_message(self) -> AssistantMessage:
        return AssistantMessage(
            text=self.text, tool_calls=self.tool_calls, provider_items=self.provider_items
        )


@dataclass(frozen=True, slots=True)
class TextDelta:
    text: str


@dataclass(frozen=True, slots=True)
class ToolCallStarted:
    """Модель начала вызов инструмента; аргументы ещё не получены (для раннего tool_started)."""

    id: str
    name: str


@dataclass(frozen=True, slots=True)
class ResponseCompleted:
    """Последний чанк стрима: клиент без состояния, финальный ответ приходит в потоке."""

    response: LLMResponse


type LLMChunk = TextDelta | ToolCallStarted | ResponseCompleted


class LLMError(DomainError):
    """Ошибка провайдера. `retryable` — таймаут, 429, 5xx: можно повторить или уйти на
    резервную модель."""

    def __init__(self, message: str, *, retryable: bool) -> None:
        super().__init__(message)
        self.retryable = retryable


class LLMClient(Protocol):
    """Стрим одного шага модели. Последний чанк — всегда ResponseCompleted; при ошибке
    провайдера поток прерывается исключением LLMError."""

    def stream(self, request: LLMRequest) -> AsyncIterator[LLMChunk]: ...
