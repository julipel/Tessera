"""Описание инструмента и вход исполнения (architecture.md §9, contracts.md §4).

Типы свои, а не из agent: зависимости идут agent → tools, порт `ToolExecutor` агента
реализуется адаптером поверх `ToolRegistry`.
"""

from collections.abc import Callable, Mapping
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


type ArgumentsCheck = Callable[[Mapping[str, Any]], str | None]
"""Проверка аргументов, которую не выразить JSON Schema: сообщение для модели или None."""

type ConfirmText = Callable[[Mapping[str, Any]], str]
"""Текст компонента confirm по аргументам вызова: что именно подтверждает пользователь."""


@dataclass(frozen=True, slots=True)
class ToolDefinition:
    """`parameters` — JSON Schema (Draft 2020-12) аргументов; `description` — для модели:
    когда и зачем вызывать. `display_label` — подпись для tool_started («Ищу в каталоге…»).

    `check` — проверка аргументов после схемы (ошибка → `validation_error`). Инструмент
    с `requires_confirmation` исполняется только после подтверждения пользователя (ADR-0021),
    `confirm_text` для него обязателен."""

    name: str
    description: str
    parameters: dict[str, Any]
    handler: ToolHandler = field(compare=False)
    timeout_s: float = 10
    side_effect: bool = False
    requires_confirmation: bool = False
    display_label: str | None = None
    check: ArgumentsCheck | None = field(default=None, compare=False)
    confirm_text: ConfirmText | None = field(default=None, compare=False)


@dataclass(frozen=True, slots=True)
class ConfirmLabels:
    """Подписи кнопок confirm — из `assistant.confirm_labels` конфига тенанта."""

    confirm: str = "Подтвердить"
    cancel: str = "Отмена"


class InvalidToolDefinitionError(DomainError):
    """Ошибка конфигурации инструментов (дубль имени, невалидная схема) — не ошибка модели."""


CONFIRM_ACTION_ID = "confirm"
CANCEL_ACTION_ID = "cancel"
"""`action_id` кнопок confirm; `payload: {confirm_id}` связывает нажатие с ожидающим вызовом."""
