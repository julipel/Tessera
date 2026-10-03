"""Вход хода агента и порт исполнения инструментов (architecture.md §5)."""

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol
from uuid import UUID

from app.modules.agent.domain.llm import LLMMessage, ToolCall, ToolSchema
from app.modules.shared.kernel import TenantId
from app.modules.tools.kernel import ToolResult


@dataclass(frozen=True, slots=True)
class TurnLimits:
    """`max_tool_retries` — сколько шагов с ошибкой валидации аргументов допускается за ход."""

    max_steps: int
    max_tool_retries: int


@dataclass(frozen=True, slots=True)
class TurnContext:
    """`history` уже содержит новое сообщение пользователя. `fallback_message` — текст мягкого
    завершения (лимит шагов, исчерпаны ретраи); берётся из AgentConfig тенанта."""

    tenant_id: TenantId
    conversation_id: UUID
    turn_id: UUID
    model: str
    system: str
    history: tuple[LLMMessage, ...]
    limits: TurnLimits
    fallback_message: str
    temperature: float | None = None


class ToolExecutor(Protocol):
    """Инструменты хода. Реализация (Tool Registry, P2-04) валидирует аргументы, исполняет
    вызовы параллельно с таймаутами и возвращает результаты в порядке `calls`; ошибки
    исполнения — в `ToolResult.error`, а не исключением. Отмена хода отменяет исполнение."""

    def schemas(self) -> tuple[ToolSchema, ...]: ...

    async def execute_many(
        self, calls: Sequence[ToolCall], ctx: TurnContext
    ) -> Sequence[ToolResult]: ...
