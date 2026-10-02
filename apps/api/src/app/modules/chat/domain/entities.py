"""Диалог и сообщения. Ввод и блоки — JSON по контрактам UserInput / MessageBlock."""

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any
from uuid import UUID

from app.modules.shared.kernel import TenantId


class Channel(StrEnum):
    WEB = "web"


class MessageRole(StrEnum):
    USER = "user"
    ASSISTANT = "assistant"


class MessageStatus(StrEnum):
    COMPLETED = "completed"
    INTERRUPTED = "interrupted"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class Conversation:
    """`agent_config_id` — версия конфига, с которой диалог начат (architecture.md §10)."""

    id: UUID
    tenant_id: TenantId
    agent_config_id: UUID
    channel: Channel
    visitor_id: str
    created_at: datetime


@dataclass(frozen=True, slots=True)
class ChatMessage:
    """`content` — плоский текст для контекста модели; `input` — только у user;
    `blocks` — только у assistant, в порядке первого появления block_id в стриме."""

    id: UUID
    tenant_id: TenantId
    conversation_id: UUID
    role: MessageRole
    status: MessageStatus
    content: str
    input: dict[str, Any] | None
    blocks: list[dict[str, Any]]
    client_message_id: UUID | None
    created_at: datetime


@dataclass(frozen=True, slots=True)
class NewMessage:
    """Данные для записи сообщения; id и created_at назначает хранилище."""

    conversation_id: UUID
    role: MessageRole
    status: MessageStatus
    content: str
    input: dict[str, Any] | None = None
    blocks: tuple[dict[str, Any], ...] = ()
    client_message_id: UUID | None = None
