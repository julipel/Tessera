"""Диалоги в админке (ADR-0038): список, разбор с заменёнными ответами и вызовами
инструментов, оценка ответа сотрудником."""

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any
from uuid import UUID

from app.modules.chat.domain.entities import ChatMessage, Conversation, ToolCallEntry

PREVIEW_CHARS = 200
MAX_PAGE = 100
MAX_COMMENT_CHARS = 2000


class Rating(StrEnum):
    GOOD = "good"
    BAD = "bad"


@dataclass(frozen=True, slots=True)
class MessageRatingEntry:
    """Оценка ответа: одна на сообщение, повторная заменяет прежнюю вместе с автором.
    `rated_by` — email автора на момент оценки (пользователь мог быть удалён)."""

    message_id: UUID
    rating: Rating
    comment: str | None
    rated_by: str | None
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class PageCursor:
    """Позиция в списке диалогов от новых к старым: следующая страница — строго раньше."""

    created_at: datetime
    id: UUID


@dataclass(frozen=True, slots=True)
class ConversationOverview:
    """Строка списка: `messages` — все сообщения, включая заменённые; `preview` — начало
    первого ввода клиента; `good`/`bad` — число ответов с такой оценкой."""

    conversation: Conversation
    agent_config_version: int
    messages: int
    last_message_at: datetime | None
    preview: str | None
    good: int
    bad: int


@dataclass(frozen=True, slots=True)
class ConversationPage:
    items: list[ConversationOverview]
    next_cursor: PageCursor | None


@dataclass(frozen=True, slots=True)
class ReviewedMessage:
    message: ChatMessage
    tool_calls: list[ToolCallEntry]
    rating: MessageRatingEntry | None


@dataclass(frozen=True, slots=True)
class ConversationReview:
    overview: ConversationOverview
    messages: list[ReviewedMessage]

    @property
    def state(self) -> dict[str, Any]:
        return self.overview.conversation.state
