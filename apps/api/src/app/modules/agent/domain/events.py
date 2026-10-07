"""События хода агента; chat переводит их в SSE и сохраняет ответ. События внутренние:
`ToolFinished` несёт аргументы и результат инструмента для записи ToolCall, а в клиент
(SSE `tool_finished`) уходят только статус и длительность."""

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from app.modules.agent.domain.llm import Usage
from app.modules.memory.kernel import DialogState
from app.modules.tools.kernel import ToolErrorCode


@dataclass(frozen=True, slots=True)
class AnswerDelta:
    """Кусок текста ассистента: ответ модели или `fallback_message` при мягком завершении."""

    text: str


@dataclass(frozen=True, slots=True)
class ToolStarted:
    """Модель начала вызов инструмента (по `ToolCallStarted` из стрима, до исполнения).
    `display_label` — подпись для посетителя («Ищу в каталоге»), если она у инструмента есть."""

    tool_call_id: str
    name: str
    display_label: str | None = None


@dataclass(frozen=True, slots=True)
class ToolFinished:
    """Вызов инструмента завершён. `arguments` — разобранные аргументы или исходная строка,
    если модель прислала не JSON-объект; `content` — результат для модели.

    `duration_ms` — длительность пакета параллельных вызовов шага (верхняя оценка вызова),
    0 — вызов отклонён без исполнения. Недетерминирована, поэтому не участвует в сравнении."""

    tool_call_id: str
    name: str
    ok: bool
    error_code: ToolErrorCode | None = None
    arguments: dict[str, Any] | str | None = None
    content: str | dict[str, Any] = ""
    error_message: str | None = None
    duration_ms: int = field(default=0, compare=False)


@dataclass(frozen=True, slots=True)
class ComponentEmitted:
    """UI-компонент из результата инструмента (contracts.md §3)."""

    component: dict[str, Any]


@dataclass(frozen=True, slots=True)
class SuggestionsOffered:
    """Быстрые ответы из результата инструмента (`suggest_replies`); в ходе действуют
    последние предложенные."""

    items: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class DialogStateUpdated:
    """Состояние диалога после шага, в котором инструменты вернули `state_patch`: полное,
    а не патч, — chat сохраняет последнее, в том числе при прерванном ходе."""

    state: DialogState


class FinishReason(StrEnum):
    ANSWERED = "answered"
    STEP_LIMIT = "step_limit"
    TOOL_RETRIES_EXHAUSTED = "tool_retries_exhausted"
    EMPTY_RESPONSE = "empty_response"


@dataclass(frozen=True, slots=True)
class TurnCompleted:
    """Последнее событие хода. `usage` — сумма по всем шагам, `steps` — число вызовов модели."""

    finish: FinishReason
    usage: Usage
    steps: int


type AgentEvent = (
    AnswerDelta
    | ToolStarted
    | ToolFinished
    | ComponentEmitted
    | SuggestionsOffered
    | DialogStateUpdated
    | TurnCompleted
)
