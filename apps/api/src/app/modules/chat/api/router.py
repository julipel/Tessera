"""Публичный API диалогов (docs/contracts.md §1)."""

from typing import Annotated
from uuid import UUID

import structlog
from fastapi import APIRouter, Depends, Request, Response, status
from fastapi.sse import EventSourceResponse

from app.contracts import (
    CreateConversationRequest,
    CreateConversationResponse,
    MessageHistory,
    SendMessageRequest,
)
from app.modules.agent.public import LLMClients, RegistryToolExecutor
from app.modules.chat.api.sse import sse_stream
from app.modules.chat.application.conversations import get_history, start_conversation
from app.modules.chat.application.turns import TurnRegistry, start_turn
from app.modules.chat.domain.errors import (
    ConversationNotFoundError,
    DuplicateMessageError,
    NoActiveConfigError,
)
from app.modules.chat.domain.ports import TurnAgent
from app.modules.chat.infrastructure.loop_agent import LoopTurnAgent
from app.modules.chat.infrastructure.repositories import (
    ConversationRepository,
    MessageRepository,
    TenantsActiveConfig,
    TenantsAgentConfigs,
    ToolCallRepository,
)
from app.modules.shared.public import ApiError, DbSession, StreamDbSession
from app.modules.tenants.public import WidgetTenant
from app.modules.tools.public import ToolRegistry

router = APIRouter(prefix="/v1/conversations", tags=["conversations"])
logger = structlog.get_logger(__name__)


def get_turn_agent(request: Request) -> TurnAgent:
    """Агентный цикл с LLM-клиентами приложения; тесты подменяют через dependency_overrides.

    Инструментов пока нет: встроенные инструменты из `tools.builtin` подключат задачи P3."""
    llms: LLMClients = request.app.state.llm_clients
    return LoopTurnAgent(llms.for_provider, RegistryToolExecutor(ToolRegistry([])))


Agent = Annotated[TurnAgent, Depends(get_turn_agent)]


def get_turn_registry(request: Request) -> TurnRegistry:
    registry: TurnRegistry = request.app.state.turn_registry
    return registry


Registry = Annotated[TurnRegistry, Depends(get_turn_registry)]


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


@router.post(
    "/{conversation_id}/messages",
    # Не response_class=EventSourceResponse: с ним FastAPI ждёт эндпоинт-генератор, а здесь
    # ошибки ввода и доступа должны уйти обычным HTTP-ответом до начала стрима.
    responses={
        200: {"description": "SSE-стрим хода", "content": {"text/event-stream": {}}},
        409: {"description": "client_message_id уже отправлен (duplicate_message)"},
    },
)
async def send_message(
    conversation_id: UUID,
    body: SendMessageRequest,
    tenant_id: WidgetTenant,
    session: StreamDbSession,
    agent: Agent,
    registry: Registry,
) -> EventSourceResponse:
    structlog.contextvars.bind_contextvars(conversation_id=str(conversation_id))
    try:
        turn = await start_turn(
            tenant_id,
            conversation_id,
            body.client_message_id,
            body.input,
            ConversationRepository(session),
            MessageRepository(session),
            ToolCallRepository(session),
            TenantsAgentConfigs(session),
            agent,
            session.commit,
            registry,
        )
    except ConversationNotFoundError as e:
        raise ApiError(status.HTTP_404_NOT_FOUND, "conversation_not_found", str(e)) from e
    except DuplicateMessageError as e:
        raise ApiError(status.HTTP_409_CONFLICT, "duplicate_message", str(e)) from e
    structlog.contextvars.bind_contextvars(turn_id=str(turn.turn_id))
    logger.info("turn_started")
    return EventSourceResponse(sse_stream(turn))


@router.post("/{conversation_id}/turns/{turn_id}/cancel", status_code=status.HTTP_204_NO_CONTENT)
async def cancel_turn(
    conversation_id: UUID, turn_id: UUID, tenant_id: WidgetTenant, registry: Registry
) -> Response:
    """Стрим хода закончится `done{interrupted}`. 404 — хода нет, он завершён или чужой."""
    structlog.contextvars.bind_contextvars(conversation_id=str(conversation_id))
    if not registry.cancel(tenant_id, conversation_id, turn_id):
        raise ApiError(status.HTTP_404_NOT_FOUND, "not_found", f"ход {turn_id} не выполняется")
    return Response(status_code=status.HTTP_204_NO_CONTENT)
