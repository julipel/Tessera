"""Вход хода агента и порт исполнения инструментов (architecture.md §5)."""

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Protocol
from uuid import UUID

from app.modules.agent.domain.llm import LLMMessage, ToolCall, ToolSchema
from app.modules.memory.kernel import DialogState
from app.modules.shared.kernel import TenantId
from app.modules.tools.kernel import ToolResult


@dataclass(frozen=True, slots=True)
class TurnLimits:
    """`max_tool_retries` — сколько шагов с ошибкой валидации аргументов допускается за ход."""

    max_steps: int
    max_tool_retries: int


@dataclass(frozen=True, slots=True)
class ConfirmationReply:
    """Пользователь нажал кнопку компонента confirm: подтвердил или отменил `confirm_id`."""

    confirm_id: str
    approved: bool


@dataclass(frozen=True, slots=True)
class TurnContext:
    """`history` уже содержит новое сообщение пользователя. `fallback_message` — текст мягкого
    завершения (лимит шагов, исчерпаны ретраи); берётся из AgentConfig тенанта. `state` —
    состояние диалога на начало хода; цикл применяет к нему `state_patch` инструментов.
    `confirmation` — ввод хода — ответ на подтверждение (ADR-0021)."""

    tenant_id: TenantId
    conversation_id: UUID
    turn_id: UUID
    model: str
    system: str
    history: tuple[LLMMessage, ...]
    limits: TurnLimits
    fallback_message: str
    temperature: float | None = None
    state: DialogState = field(default_factory=DialogState)
    confirmation: ConfirmationReply | None = None


class ToolExecutor(Protocol):
    """Инструменты хода. Реализация (Tool Registry, P2-04) валидирует аргументы, исполняет
    вызовы параллельно с таймаутами и возвращает результаты в порядке `calls`; ошибки
    исполнения — в `ToolResult.error`, а не исключением. Отмена хода отменяет исполнение."""

    def schemas(self) -> tuple[ToolSchema, ...]: ...

    def display_label(self, name: str) -> str | None:
        """Подпись инструмента для статуса в чате; None — нет подписи или инструмента."""
        ...

    async def execute_many(
        self, calls: Sequence[ToolCall], ctx: TurnContext
    ) -> Sequence[ToolResult]: ...

    async def execute_confirmed(self, call: ToolCall, ctx: TurnContext) -> ToolResult:
        """Вызов, подтверждённый пользователем: исполняется без запроса подтверждения."""
        ...
