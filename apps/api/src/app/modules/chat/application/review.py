"""Диалоги в админке (ADR-0038): список страницами, разбор, оценка ответа сотрудником."""

import base64
import binascii
from datetime import datetime
from uuid import UUID

from app.modules.chat.domain.entities import MessageRole
from app.modules.chat.domain.errors import (
    ConversationNotFoundError,
    InvalidCursorError,
    MessageNotFoundError,
    NotAssistantMessageError,
)
from app.modules.chat.domain.ports import ConversationReviewStore
from app.modules.chat.domain.review import (
    MAX_PAGE,
    ConversationPage,
    ConversationReview,
    MessageRatingEntry,
    PageCursor,
    Rating,
)
from app.modules.shared.kernel import AdminPrincipal, TenantId


def encode_cursor(cursor: PageCursor) -> str:
    raw = f"{cursor.created_at.isoformat()}|{cursor.id}"
    return base64.urlsafe_b64encode(raw.encode()).decode().rstrip("=")


def decode_cursor(value: str) -> PageCursor:
    try:
        raw = base64.urlsafe_b64decode(value + "=" * (-len(value) % 4)).decode()
        created_at, _, id_ = raw.partition("|")
        cursor = PageCursor(datetime.fromisoformat(created_at), UUID(id_))
    except (binascii.Error, UnicodeDecodeError, ValueError) as e:
        raise InvalidCursorError("неверный курсор страницы") from e
    if cursor.created_at.tzinfo is None:
        raise InvalidCursorError("неверный курсор страницы")
    return cursor


async def list_conversations(
    tenant_id: TenantId,
    store: ConversationReviewStore,
    *,
    before: str | None,
    limit: int,
    rating: Rating | None,
) -> ConversationPage:
    """Страница от новых к старым; лишняя строка показывает, есть ли следующая."""
    limit = max(1, min(limit, MAX_PAGE))
    items = await store.list_conversations(
        tenant_id,
        before=decode_cursor(before) if before else None,
        limit=limit + 1,
        rating=rating,
    )
    page, rest = items[:limit], items[limit:]
    last = page[-1].conversation if rest else None
    return ConversationPage(page, PageCursor(last.created_at, last.id) if last else None)


async def review_conversation(
    tenant_id: TenantId, conversation_id: UUID, store: ConversationReviewStore
) -> ConversationReview:
    review = await store.review(tenant_id, conversation_id)
    if review is None:
        raise ConversationNotFoundError(f"диалог {conversation_id} не найден")
    return review


async def rate_message(
    tenant_id: TenantId,
    conversation_id: UUID,
    message_id: UUID,
    rating: Rating,
    comment: str | None,
    author: AdminPrincipal,
    store: ConversationReviewStore,
) -> MessageRatingEntry:
    """Оценка ответа ассистента; повторная заменяет прежнюю и её автора. Пустой
    комментарий хранится как None."""
    await _assistant_message(tenant_id, conversation_id, message_id, store)
    return await store.put_rating(
        tenant_id,
        conversation_id,
        message_id,
        rating,
        (comment or "").strip() or None,
        rated_by=author.user_id,
        rated_by_email=author.email,
    )


async def clear_rating(
    tenant_id: TenantId,
    conversation_id: UUID,
    message_id: UUID,
    store: ConversationReviewStore,
) -> None:
    """Снять оценку; если её нет — ничего не делать."""
    await _assistant_message(tenant_id, conversation_id, message_id, store)
    await store.delete_rating(tenant_id, message_id)


async def _assistant_message(
    tenant_id: TenantId,
    conversation_id: UUID,
    message_id: UUID,
    store: ConversationReviewStore,
) -> None:
    message = await store.find_message(tenant_id, conversation_id, message_id)
    if message is None:
        raise MessageNotFoundError(f"сообщение {message_id} не найдено")
    if message.role is not MessageRole.ASSISTANT:
        raise NotAssistantMessageError("оценить можно только ответ ассистента")
