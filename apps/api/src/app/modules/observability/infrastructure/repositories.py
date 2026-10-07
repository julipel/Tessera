"""Репозиторий журнала событий хода."""

from collections.abc import Sequence
from uuid import UUID

from sqlalchemy import ColumnElement

from app.modules.observability.domain.events import AgentEventEntry
from app.modules.observability.infrastructure.models import AgentEventRecord
from app.modules.shared.public import TenantId, TenantRepository


class AgentEventRepository(TenantRepository[AgentEventRecord]):
    model = AgentEventRecord

    async def add_many(self, tenant_id: TenantId, entries: Sequence[AgentEventEntry]) -> None:
        self.session.add_all(
            AgentEventRecord(
                tenant_id=tenant_id,
                conversation_id=e.conversation_id,
                turn_id=e.turn_id,
                trace_id=e.trace_id,
                seq=e.seq,
                type=e.type,
                payload=e.payload,
                ts=e.ts,
            )
            for e in entries
        )
        await self.session.flush()

    async def find(
        self,
        tenant_id: TenantId,
        conversation_id: UUID | None = None,
        turn_id: UUID | None = None,
        trace_id: str | None = None,
        limit: int = 1000,
    ) -> list[AgentEventEntry]:
        """События тенанта по фильтрам (заданные — все сразу) в порядке ходов и событий."""
        criteria: list[ColumnElement[bool]] = []
        if conversation_id is not None:
            criteria.append(AgentEventRecord.conversation_id == conversation_id)
        if turn_id is not None:
            criteria.append(AgentEventRecord.turn_id == turn_id)
        if trace_id is not None:
            criteria.append(AgentEventRecord.trace_id == trace_id)
        stmt = (
            self._scoped(tenant_id)
            .where(*criteria)
            .order_by(AgentEventRecord.ts, AgentEventRecord.seq)
            .limit(limit)
        )
        return [
            AgentEventEntry(
                conversation_id=r.conversation_id,
                turn_id=r.turn_id,
                trace_id=r.trace_id,
                seq=r.seq,
                type=r.type,
                payload=r.payload,
                ts=r.ts,
            )
            for r in (await self.session.execute(stmt)).scalars()
        ]
