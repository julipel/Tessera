"""Публичный API диалогов (docs/contracts.md §1)."""

from uuid import UUID

import structlog
from fastapi import APIRouter, status

from app.contracts import CreateConversationRequest, CreateConversationResponse, MessageHistory
from app.modules.chat.application.conversations import get_history, start_conversation
from app.modules.chat.domain.errors import ConversationNotFoundError, NoActiveConfigError
from app.modules.chat.infrastructure.repositories import (
    ConversationRepository,
    MessageRepository,
    TenantsActiveConfig,
)
from app.modules.shared.public import ApiError, DbSession
from app.modules.tenants.public import WidgetTenant

router = APIRouter(prefix="/v1/conversations", tags=["conversations"])
logger = structlog.get_logger(__name__)


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_conversation(
    body: CreateConversationRequest, tenant_id: WidgetTenant, session: DbSession
) -> CreateConversationResponse:
    try:
        conversation = await start_conversation(
            tenant_id,
            body.visitor_id,
            ConversationRepository(session),
            TenantsActiveConfig(session),
        )
    except NoActiveConfigError as e:
        raise ApiError(status.HTTP_404_NOT_FOUND, "not_found", str(e)) from e
    structlog.contextvars.bind_contextvars(conversation_id=str(conversation.id))
    logger.info("conversation_created", agent_config_id=str(conversation.agent_config_id))
    return CreateConversationResponse(conversation_id=conversation.id)


# exclude_none: у сообщения ассистента нет `input` — поле опускается, а не отдаётся null.
@router.get("/{conversation_id}/messages", response_model_exclude_none=True)
async def conversation_messages(
    conversation_id: UUID, tenant_id: WidgetTenant, session: DbSession
) -> MessageHistory:
    structlog.contextvars.bind_contextvars(conversation_id=str(conversation_id))
    try:
        return await get_history(
            tenant_id, conversation_id, ConversationRepository(session), MessageRepository(session)
        )
    except ConversationNotFoundError as e:
        raise ApiError(status.HTTP_404_NOT_FOUND, "conversation_not_found", str(e)) from e
