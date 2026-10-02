"""Порты хранилищ модуля chat (реализации — в infrastructure)."""

from collections.abc import AsyncIterator, Sequence
from typing import Protocol
from uuid import UUID

from app.modules.chat.domain.entities import (
    AgentTextDelta,
    Channel,
    ChatMessage,
    Conversation,
    NewMessage,
    TurnRequest,
)
from app.modules.shared.kernel import TenantId


class ConversationStore(Protocol):
    async def create(
        self, tenant_id: TenantId, agent_config_id: UUID, channel: Channel, visitor_id: str
    ) -> Conversation: ...

    async def find(self, tenant_id: TenantId, conversation_id: UUID) -> Conversation | None: ...


class MessageStore(Protocol):
    async def list_for(self, tenant_id: TenantId, conversation_id: UUID) -> Sequence[ChatMessage]:
        """Сообщения диалога в порядке создания."""
        ...

    async def add_once(self, tenant_id: TenantId, message: NewMessage) -> tuple[ChatMessage, bool]:
        """Записать сообщение; если в диалоге уже есть сообщение с тем же client_message_id —
        вернуть существующее. Второй элемент — создано ли новое."""
        ...


class ActiveConfigLookup(Protocol):
    """Активная версия AgentConfig тенанта (данные модуля tenants)."""

    async def active_config_id(self, tenant_id: TenantId) -> UUID | None: ...


class TurnAgent(Protocol):
    """Агент, отвечающий на ход пользователя потоком событий (до P2-08 — эхо-заглушка)."""

    def run_turn(self, request: TurnRequest) -> AsyncIterator[AgentTextDelta]: ...
