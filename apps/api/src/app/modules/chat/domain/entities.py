"""Диалог и сообщения. Ввод и блоки — JSON по контрактам UserInput / MessageBlock."""

from dataclasses import dataclass, field
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
    """`agent_config_id` — версия конфига, с которой диалог начат (architecture.md §10);
    `state` — DialogState в JSON (architecture.md §7); `summary` — сводка ранней истории,
    `summary_message_id` — последнее свёрнутое в неё сообщение (P6-02); `language` — язык
    диалога, выбранный при создании (ADR-0025; None — диалог создан до P6-04)."""

    id: UUID
    tenant_id: TenantId
    agent_config_id: UUID
    channel: Channel
    visitor_id: str
    created_at: datetime
    state: dict[str, Any] = field(default_factory=dict)
    summary: str | None = None
    summary_message_id: UUID | None = None
    language: str | None = None


@dataclass(frozen=True, slots=True)
class ActiveConfig:
    """Активная версия AgentConfig тенанта: id и JSON конфига."""

    id: UUID
    config: dict[str, Any]


@dataclass(frozen=True, slots=True)
class ChatMessage:
    """`content` — плоский текст для контекста модели; `input` — только у user;
    `blocks` — только у assistant, в порядке первого появления block_id в стриме;
    `error` — `{code, message, retryable}` у неудачного ответа (как SSE-событие `error`)."""

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
    error: dict[str, Any] | None = None


@dataclass(frozen=True, slots=True)
class NewMessage:
    """Данные для записи сообщения; created_at (и id, если не задан) назначает хранилище."""

    conversation_id: UUID
    role: MessageRole
    status: MessageStatus
    content: str
    input: dict[str, Any] | None = None
    blocks: tuple[dict[str, Any], ...] = ()
    client_message_id: UUID | None = None
    id: UUID | None = None
    error: dict[str, Any] | None = None


@dataclass(frozen=True, slots=True)
class ToolCallEntry:
    """Вызов инструмента в ответе ассистента (таблица tool_calls). В клиент не отдаётся.

    `tool_call_id` — id вызова у провайдера; `arguments` — разобранные аргументы или исходная
    строка, если модель прислала не JSON-объект; `result` — результат для модели (None при
    ошибке); `error` — `{code, message}`."""

    tool_call_id: str
    name: str
    arguments: dict[str, Any] | str
    result: str | dict[str, Any] | None
    error: dict[str, str] | None
    duration_ms: int


@dataclass(frozen=True, slots=True)
class TurnRequest:
    """Вход хода агента: `input` — ввод пользователя по контракту UserInput, `agent_config` —
    версия AgentConfig диалога, `history` — сообщения диалога после сводки, включая этот
    ввод, `history_summary` — сводка более ранних сообщений, `dialog_state` — состояние
    диалога на начало хода, `language` — язык диалога (None — выбрать по конфигу).
    Всё загружено до запуска агента:
    задача агента отменяема и в БД не ходит."""

    tenant_id: TenantId
    conversation_id: UUID
    agent_config_id: UUID
    turn_id: UUID
    input: dict[str, Any]
    agent_config: dict[str, Any]
    history: tuple[ChatMessage, ...]
    dialog_state: dict[str, Any] = field(default_factory=dict)
    history_summary: str | None = None
    language: str | None = None
