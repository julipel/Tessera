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
from app.modules.agent.public import LLMClients
from app.modules.chat.api.sse import sse_stream
from app.modules.chat.application.conversations import get_history, start_conversation
from app.modules.chat.application.summaries import SummaryScheduler
from app.modules.chat.application.turns import TurnRegistry, retry_turn, start_turn
from app.modules.chat.domain.errors import (
    ConversationNotFoundError,
    DuplicateMessageError,
    InvalidInputError,
    MessageNotFoundError,
    NoActiveConfigError,
    RetryNotAllowedError,
)
from app.modules.chat.domain.ports import TurnAgent
from app.modules.chat.infrastructure.loop_agent import builtin_turn_agent
from app.modules.chat.infrastructure.repositories import (
    ConversationRepository,
    MessageRepository,
    TenantsActiveConfig,
    TenantsAgentConfigs,
    ToolCallRepository,
)
from app.modules.knowledge.public import KnowledgeSearch, SqlCatalog
from app.modules.leads.public import SqlLeadStore
from app.modules.observability.public import AgentEventRepository, Tracer
from app.modules.shared.public import (
    ApiError,
    DbSession,
    RateLimit,
    RateLimiter,
    StreamDbSession,
    client_ip,
    enforce_rate_limits,
)
from app.modules.tenants.public import WidgetTenant

router = APIRouter(prefix="/v1/conversations", tags=["conversations"])
logger = structlog.get_logger(__name__)


def get_turn_agent(request: Request) -> TurnAgent:
    """Агентный цикл с LLM-клиентами приложения и встроенными инструментами из
    `tools.builtin` конфига (поиск по знаниям — если собран; каталог; заявки); тесты
    подменяют через dependency_overrides."""
    llms: LLMClients = request.app.state.llm_clients
    knowledge: KnowledgeSearch | None = request.app.state.knowledge_search
    catalog: SqlCatalog = request.app.state.catalog
    leads: SqlLeadStore = request.app.state.leads
    tracer: Tracer = request.app.state.tracer
    return builtin_turn_agent(llms.for_provider, knowledge, catalog, leads, tracer)


Agent = Annotated[TurnAgent, Depends(get_turn_agent)]


def get_turn_registry(request: Request) -> TurnRegistry:
    registry: TurnRegistry = request.app.state.turn_registry
    return registry


Registry = Annotated[TurnRegistry, Depends(get_turn_registry)]


def get_summary_scheduler(request: Request) -> SummaryScheduler:
    """Фоновые сводки процесса; тесты подменяют через dependency_overrides."""
    scheduler: SummaryScheduler = request.app.state.summary_scheduler
    return scheduler


Summaries = Annotated[SummaryScheduler, Depends(get_summary_scheduler)]


def _trace_id(request: Request) -> str:
    """trace_id запроса (TraceIdMiddleware) — в журнал хода: ход идёт в этом запросе."""
    trace_id: str = getattr(request.state, "trace_id", "")
    return trace_id


# Ввод хода: текст — до 4000 символов по контракту; тело целиком (payload кнопки, значения
# формы) — не больше этого (ADR-0030).
MAX_TURN_BODY_BYTES = 32 * 1024


def _limiter(request: Request) -> RateLimiter:
    limiter: RateLimiter = request.app.state.rate_limiter
    return limiter


async def limit_conversations(request: Request) -> None:
    """Лимит создания диалогов с одного IP: `visitor_id` задаёт клиент, его легко менять."""
    settings = request.app.state.settings
    rule = RateLimit("conversations_ip", settings.rate_conversations_per_ip_per_hour, 3600)
    await enforce_rate_limits(_limiter(request), [(rule, client_ip(request))])


async def limit_turns(request: Request, tenant_id: WidgetTenant) -> None:
    """Лимит ходов (сообщения и повторы): с одного IP и на тенанта (расход LLM)."""
    settings = request.app.state.settings
    await enforce_rate_limits(
        _limiter(request),
        [
            (RateLimit("turns_ip", settings.rate_turns_per_ip_per_min, 60), client_ip(request)),
            (
                RateLimit("turns_tenant", settings.rate_turns_per_tenant_per_min, 60),
                str(tenant_id),
            ),
        ],
    )


async def limit_turn_body(request: Request) -> None:
    """Слишком большое тело хода — 422 `invalid_input` до разбора и записи."""
    length = request.headers.get("content-length", "")
    too_long = length.isdigit() and int(length) > MAX_TURN_BODY_BYTES
    if too_long or len(await request.body()) > MAX_TURN_BODY_BYTES:
        raise ApiError(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            "invalid_input",
            f"тело запроса больше {MAX_TURN_BODY_BYTES} байт",
        )


@router.post(
    "",
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(limit_conversations)],
    responses={429: {"description": "лимит частоты (rate_limited), заголовок Retry-After"}},
)
async def create_conversation(
    body: CreateConversationRequest, tenant_id: WidgetTenant, session: DbSession
) -> CreateConversationResponse:
    try:
        conversation = await start_conversation(
            tenant_id,
            body.visitor_id,
            ConversationRepository(session),
            TenantsActiveConfig(session),
            body.locale,
        )
    except NoActiveConfigError as e:
        raise ApiError(status.HTTP_404_NOT_FOUND, "not_found", str(e)) from e
    structlog.contextvars.bind_contextvars(conversation_id=str(conversation.id))
    logger.info(
        "conversation_created",
        agent_config_id=str(conversation.agent_config_id),
        language=conversation.language,
    )
    return CreateConversationResponse(conversation_id=conversation.id)


# exclude_none: у сообщения ассистента нет `input`, у успешного ответа — `error`: поле
# опускается, а не отдаётся null.
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
        429: {"description": "лимит частоты (rate_limited), заголовок Retry-After"},
    },
    dependencies=[Depends(limit_turn_body), Depends(limit_turns)],
)
async def send_message(
    request: Request,
    conversation_id: UUID,
    body: SendMessageRequest,
    tenant_id: WidgetTenant,
    session: StreamDbSession,
    agent: Agent,
    registry: Registry,
    summaries: Summaries,
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
            summaries,
            agent_events=AgentEventRepository(session),
            trace_id=_trace_id(request),
        )
    except ConversationNotFoundError as e:
        raise ApiError(status.HTTP_404_NOT_FOUND, "conversation_not_found", str(e)) from e
    except DuplicateMessageError as e:
        raise ApiError(status.HTTP_409_CONFLICT, "duplicate_message", str(e)) from e
    except InvalidInputError as e:
        raise ApiError(status.HTTP_422_UNPROCESSABLE_CONTENT, "invalid_input", str(e)) from e
    structlog.contextvars.bind_contextvars(turn_id=str(turn.turn_id))
    logger.info("turn_started")
    return EventSourceResponse(sse_stream(turn))


@router.post(
    "/{conversation_id}/messages/{message_id}/retry",
    responses={
        200: {"description": "SSE-стрим нового хода", "content": {"text/event-stream": {}}},
        404: {"description": "диалога (conversation_not_found) или ответа (not_found) нет"},
        409: {"description": "ответ нельзя повторить (not_retryable)"},
        429: {"description": "лимит частоты (rate_limited), заголовок Retry-After"},
    },
    dependencies=[Depends(limit_turns)],
)
async def retry_message(
    request: Request,
    conversation_id: UUID,
    message_id: UUID,
    tenant_id: WidgetTenant,
    session: StreamDbSession,
    agent: Agent,
    registry: Registry,
    summaries: Summaries,
) -> EventSourceResponse:
    """Повтор неудачного ответа `message_id` (ADR-0023): ход по тому же вводу, ответ
    заменяет неудачный в истории и контексте модели."""
    structlog.contextvars.bind_contextvars(conversation_id=str(conversation_id))
    try:
        turn = await retry_turn(
            tenant_id,
            conversation_id,
            message_id,
            ConversationRepository(session),
            MessageRepository(session),
            ToolCallRepository(session),
            TenantsAgentConfigs(session),
            agent,
            session.commit,
            registry,
            summaries,
            agent_events=AgentEventRepository(session),
            trace_id=_trace_id(request),
        )
    except ConversationNotFoundError as e:
        raise ApiError(status.HTTP_404_NOT_FOUND, "conversation_not_found", str(e)) from e
    except MessageNotFoundError as e:
        raise ApiError(status.HTTP_404_NOT_FOUND, "not_found", str(e)) from e
    except RetryNotAllowedError as e:
        raise ApiError(status.HTTP_409_CONFLICT, "not_retryable", str(e)) from e
    structlog.contextvars.bind_contextvars(turn_id=str(turn.turn_id))
    logger.info("turn_started", retry_of=str(message_id))
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
