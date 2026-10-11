"""API админки: диалоги тенанта, разбор и оценка ответов (docs/contracts.md §7, ADR-0038).
Доступ — роль viewer в тенанте."""

from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Query, status

from app.contracts import (
    AdminMessage,
    ConversationDetail,
    ConversationList,
    ConversationSummary,
    MessageRating,
    RateMessageRequest,
)
from app.modules.chat.application.review import (
    clear_rating,
    encode_cursor,
    list_conversations,
    rate_message,
    review_conversation,
)
from app.modules.chat.domain.errors import (
    ConversationNotFoundError,
    InvalidCursorError,
    MessageNotFoundError,
    NotAssistantMessageError,
)
from app.modules.chat.domain.review import (
    MAX_PAGE,
    ConversationOverview,
    MessageRatingEntry,
    Rating,
    ReviewedMessage,
)
from app.modules.chat.infrastructure.review_store import SqlConversationReviewStore
from app.modules.shared.public import (
    AdminPrincipal,
    AdminViewer,
    ApiError,
    DbSession,
    TenantId,
)

router = APIRouter(prefix="/v1/admin/tenants/{tenant_id}/conversations", tags=["admin"])

Viewer = Annotated[AdminPrincipal, AdminViewer]


@router.get("", dependencies=[AdminViewer])
async def conversations(
    tenant_id: UUID,
    session: DbSession,
    before: Annotated[str | None, Query(max_length=200)] = None,
    limit: Annotated[int, Query(ge=1, le=MAX_PAGE)] = 50,
    rating: Rating | None = None,
) -> ConversationList:
    """От новых к старым; `before` — `next_before` предыдущей страницы; `rating` — диалоги,
    где есть ответ с такой оценкой."""
    try:
        page = await list_conversations(
            TenantId(tenant_id),
            SqlConversationReviewStore(session),
            before=before,
            limit=limit,
            rating=rating,
        )
    except InvalidCursorError as e:
        raise ApiError(status.HTTP_422_UNPROCESSABLE_CONTENT, "invalid_input", str(e)) from e
    return ConversationList(
        conversations=[_summary(o) for o in page.items],
        next_before=encode_cursor(page.next_cursor) if page.next_cursor else None,
    )


# exclude_unset: у сообщения ассистента нет `input`, у успешного ответа — `error`: поле
# опускается, как в публичной истории; `replaced_by` и `rating` отдаются и null.
@router.get("/{conversation_id}", dependencies=[AdminViewer], response_model_exclude_unset=True)
async def conversation(
    tenant_id: UUID, conversation_id: UUID, session: DbSession
) -> ConversationDetail:
    try:
        review = await review_conversation(
            TenantId(tenant_id), conversation_id, SqlConversationReviewStore(session)
        )
    except ConversationNotFoundError as e:
        raise ApiError(status.HTTP_404_NOT_FOUND, "not_found", "диалог не найден") from e
    return ConversationDetail(
        conversation=_summary(review.overview),
        state=review.state,
        history_summary=review.overview.conversation.summary,
        messages=[_message(m) for m in review.messages],
    )


@router.put("/{conversation_id}/messages/{message_id}/rating")
async def put_rating(
    tenant_id: UUID,
    conversation_id: UUID,
    message_id: UUID,
    body: RateMessageRequest,
    session: DbSession,
    principal: Viewer,
) -> MessageRating:
    """Оценка ответа ассистента; повторная заменяет прежнюю. Сообщение клиента — 422."""
    try:
        entry = await rate_message(
            TenantId(tenant_id),
            conversation_id,
            message_id,
            Rating(body.rating),
            body.comment,
            principal,
            SqlConversationReviewStore(session),
        )
    except (MessageNotFoundError, NotAssistantMessageError) as e:
        raise _message_problem(e) from e
    return _rating(entry)


@router.delete(
    "/{conversation_id}/messages/{message_id}/rating",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[AdminViewer],
)
async def delete_rating(
    tenant_id: UUID, conversation_id: UUID, message_id: UUID, session: DbSession
) -> None:
    """Снять оценку; если её нет — тоже 204."""
    try:
        await clear_rating(
            TenantId(tenant_id), conversation_id, message_id, SqlConversationReviewStore(session)
        )
    except (MessageNotFoundError, NotAssistantMessageError) as e:
        raise _message_problem(e) from e


def _summary(o: ConversationOverview) -> ConversationSummary:
    c = o.conversation
    return ConversationSummary.model_validate(
        {
            "id": c.id,
            "created_at": c.created_at,
            "last_message_at": o.last_message_at,
            "channel": c.channel,
            "language": c.language,
            "agent_config_id": c.agent_config_id,
            "agent_config_version": o.agent_config_version,
            "messages": o.messages,
            "preview": o.preview,
            "ratings": {"good": o.good, "bad": o.bad},
        }
    )


def _message(item: ReviewedMessage) -> AdminMessage:
    m = item.message
    data: dict[str, Any] = {
        "message_id": m.id,
        "role": m.role,
        "status": m.status,
        "created_at": m.created_at,
        "blocks": m.blocks,
        "replaced_by": m.replaced_by,
        "tool_calls": [
            {
                "tool_call_id": c.tool_call_id,
                "name": c.name,
                "arguments": c.arguments,
                "result": c.result,
                "error": c.error,
                "duration_ms": c.duration_ms,
            }
            for c in item.tool_calls
        ],
        "rating": _rating(item.rating) if item.rating else None,
    }
    if m.input is not None:
        data["input"] = m.input
    if m.error is not None:
        data["error"] = m.error
    return AdminMessage.model_validate(data)


def _rating(entry: MessageRatingEntry) -> MessageRating:
    return MessageRating(
        rating=entry.rating.value,
        comment=entry.comment,
        rated_by=entry.rated_by,
        updated_at=entry.updated_at,
    )


def _message_problem(e: MessageNotFoundError | NotAssistantMessageError) -> ApiError:
    if isinstance(e, NotAssistantMessageError):
        return ApiError(status.HTTP_422_UNPROCESSABLE_CONTENT, "invalid_input", str(e))
    return ApiError(status.HTTP_404_NOT_FOUND, "not_found", "сообщение не найдено")
