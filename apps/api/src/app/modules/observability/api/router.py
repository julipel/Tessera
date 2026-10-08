"""API админки: журнал событий хода (docs/contracts.md §7); доступ — роль viewer в тенанте."""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Query, status

from app.contracts import AgentEventItem, AgentEventList
from app.modules.observability.infrastructure.repositories import AgentEventRepository
from app.modules.shared.public import AdminViewer, ApiError, DbSession, TenantId

router = APIRouter(prefix="/v1/admin", tags=["admin"], dependencies=[AdminViewer])

MAX_EVENTS = 1000


@router.get("/tenants/{tenant_id}/events")
async def agent_events(
    tenant_id: UUID,
    session: DbSession,
    conversation_id: UUID | None = None,
    turn_id: UUID | None = None,
    trace_id: Annotated[str | None, Query(min_length=1, max_length=128)] = None,
) -> AgentEventList:
    """События ходов тенанта по диалогу, ходу или trace_id (`X-Trace-Id` ответа); нужен хотя
    бы один фильтр, заданные применяются вместе. Не больше 1000 событий."""
    if conversation_id is None and turn_id is None and trace_id is None:
        raise ApiError(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            "invalid_input",
            "нужен conversation_id, turn_id или trace_id",
        )
    entries = await AgentEventRepository(session).find(
        TenantId(tenant_id), conversation_id, turn_id, trace_id, limit=MAX_EVENTS
    )
    return AgentEventList(
        events=[
            AgentEventItem(
                conversation_id=e.conversation_id,
                turn_id=e.turn_id,
                trace_id=e.trace_id,
                seq=e.seq,
                type=e.type,  # типы пишет только chat (TurnJournal)
                payload=e.payload,
                ts=e.ts,
            )
            for e in entries
        ]
    )
