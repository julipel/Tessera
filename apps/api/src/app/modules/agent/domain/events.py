"""События хода агента; chat переводит их в SSE (P2-08). Сырые результаты инструментов
сюда не попадают — только статус и UI-компоненты."""

from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from app.modules.agent.domain.llm import Usage
from app.modules.tools.public import ToolErrorCode


@dataclass(frozen=True, slots=True)
class AnswerDelta:
    """Кусок текста ассистента: ответ модели или `fallback_message` при мягком завершении."""

    text: str


@dataclass(frozen=True, slots=True)
class ToolStarted:
    """Модель начала вызов инструмента (по `ToolCallStarted` из стрима, до исполнения)."""

    tool_call_id: str
    name: str


@dataclass(frozen=True, slots=True)
class ToolFinished:
    tool_call_id: str
    name: str
    ok: bool
    error_code: ToolErrorCode | None = None


@dataclass(frozen=True, slots=True)
class ComponentEmitted:
    """UI-компонент из результата инструмента (contracts.md §3)."""

    component: dict[str, Any]


class FinishReason(StrEnum):
    ANSWERED = "answered"
    STEP_LIMIT = "step_limit"
    TOOL_RETRIES_EXHAUSTED = "tool_retries_exhausted"


@dataclass(frozen=True, slots=True)
class TurnCompleted:
    """Последнее событие хода. `usage` — сумма по всем шагам, `steps` — число вызовов модели."""

    finish: FinishReason
    usage: Usage
    steps: int


type AgentEvent = AnswerDelta | ToolStarted | ToolFinished | ComponentEmitted | TurnCompleted
