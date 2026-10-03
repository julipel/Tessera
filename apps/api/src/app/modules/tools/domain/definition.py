"""Описание инструмента и вход исполнения (architecture.md §9, contracts.md §4).

Типы свои, а не из agent: зависимости идут agent → tools, порт `ToolExecutor` агента
реализуется адаптером поверх `ToolRegistry`.
"""

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol
from uuid import UUID

from app.modules.shared.kernel import DomainError, TenantId
from app.modules.tools.domain.result import ToolResult


@dataclass(frozen=True, slots=True)
class ToolContext:
    """Чей ход исполняет инструмент: обработчики ходят в данные только с этим `tenant_id`."""

    tenant_id: TenantId
    conversation_id: UUID
    turn_id: UUID


@dataclass(frozen=True, slots=True)
class ToolInvocation:
    """Вызов инструмента; `arguments` — уже разобранный JSON-объект, ещё не провалидированный."""

    id: str
    name: str
    arguments: Mapping[str, Any]


class ToolHandler(Protocol):
    """Исполнитель инструмента. Аргументы уже прошли JSON Schema. Ожидаемые ошибки
    (не найдено, внешний API недоступен) — в `ToolResult.error`; исключение реестр
    превратит в `upstream_error`."""

    async def __call__(self, arguments: Mapping[str, Any], ctx: ToolContext, /) -> ToolResult: ...


@dataclass(frozen=True, slots=True)
class ToolDefinition:
    """`parameters` — JSON Schema (Draft 2020-12) аргументов; `description` — для модели:
    когда и зачем вызывать. `display_label` — подпись для tool_started («Ищу в каталоге…»)."""

    name: str
    description: str
    parameters: dict[str, Any]
    handler: ToolHandler = field(compare=False)
    timeout_s: float = 10
    side_effect: bool = False
    requires_confirmation: bool = False
    display_label: str | None = None


class InvalidToolDefinitionError(DomainError):
    """Ошибка конфигурации инструментов (дубль имени, невалидная схема) — не ошибка модели."""
