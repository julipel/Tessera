"""Порты хранилищ модуля chat (реализации — в infrastructure)."""

from collections.abc import AsyncIterator, Sequence
from typing import Any, Protocol
from uuid import UUID

from app.modules.agent.kernel import AgentEvent
from app.modules.chat.domain.entities import (
    Channel,
    ChatMessage,
    Conversation,
    NewMessage,
    ToolCallEntry,
    TurnRequest,
)
from app.modules.shared.kernel import TenantId


class ConversationStore(Protocol):
    async def create(
        self, tenant_id: TenantId, agent_config_id: UUID, channel: Channel, visitor_id: str
    ) -> Conversation: ...

    async def find(self, tenant_id: TenantId, conversation_id: UUID) -> Conversation | None: ...

    async def update_state(
        self, tenant_id: TenantId, conversation_id: UUID, state: dict[str, Any]
    ) -> None: ...


class MessageStore(Protocol):
    async def list_for(self, tenant_id: TenantId, conversation_id: UUID) -> Sequence[ChatMessage]:
        """Сообщения диалога в порядке создания, кроме заменённых повтором (ADR-0023)."""
        ...

    async def mark_replaced(self, tenant_id: TenantId, message_id: UUID, replaced_by: UUID) -> bool:
        """Пометить сообщение заменённым ответом `replaced_by`. False — сообщения нет или оно
        уже заменено: из двух одновременных повторов пометку получает только один."""
        ...

    async def add_once(self, tenant_id: TenantId, message: NewMessage) -> tuple[ChatMessage, bool]:
        """Записать сообщение; если в диалоге уже есть сообщение с тем же client_message_id —
        вернуть существующее. Второй элемент — создано ли новое."""
        ...


class ToolCallStore(Protocol):
    async def add_many(
        self, tenant_id: TenantId, message_id: UUID, calls: Sequence[ToolCallEntry]
    ) -> None: ...

    async def list_for(self, tenant_id: TenantId, message_id: UUID) -> Sequence[ToolCallEntry]:
        """Вызовы сообщения в порядке записи."""
        ...


class ActiveConfigLookup(Protocol):
    """Активная версия AgentConfig тенанта (данные модуля tenants)."""

    async def active_config_id(self, tenant_id: TenantId) -> UUID | None: ...


class AgentConfigSource(Protocol):
    """Конкретная версия AgentConfig тенанта (данные модуля tenants)."""

    async def config(self, tenant_id: TenantId, config_id: UUID) -> dict[str, Any] | None: ...


class TurnAgent(Protocol):
    """Агент, отвечающий на ход пользователя потоком событий (docs/architecture.md §5).

    Ошибка провайдера — исключение `LLMError` из потока; остальные исключения — сбой хода."""

    def run_turn(self, request: TurnRequest) -> AsyncIterator[AgentEvent]: ...
