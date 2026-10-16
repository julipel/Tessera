"""Репозиторий журнала событий хода."""

from collections.abc import Sequence
from datetime import datetime
from uuid import UUID

from sqlalchemy import ColumnElement, tuple_

from app.modules.observability.domain.events import AgentEventEntry, EventCursor
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

    async def purge_before(self, tenant_id: TenantId, before: datetime, limit: int) -> int:
        """Порция событий журнала старше `before` (срок хранения, ADR-0043)."""
        return await self._delete_batch(tenant_id, AgentEventRecord.ts < before, limit=limit)

    async def count_before(self, tenant_id: TenantId, before: datetime) -> int:
        return await self._count(tenant_id, AgentEventRecord.ts < before)

    async def find(
        self,
        tenant_id: TenantId,
        conversation_id: UUID | None = None,
        turn_id: UUID | None = None,
        trace_id: str | None = None,
        limit: int = 1000,
        after: EventCursor | None = None,
    ) -> list[AgentEventEntry]:
        """События тенанта по фильтрам (заданные — все сразу) в порядке `(ts, turn_id, seq)`;
        `after` — продолжить после этого события (страницы без OFFSET)."""
        criteria: list[ColumnElement[bool]] = []
        if conversation_id is not None:
            criteria.append(AgentEventRecord.conversation_id == conversation_id)
        if turn_id is not None:
            criteria.append(AgentEventRecord.turn_id == turn_id)
        if trace_id is not None:
            criteria.append(AgentEventRecord.trace_id == trace_id)
        if after is not None:
            criteria.append(
                tuple_(AgentEventRecord.ts, AgentEventRecord.turn_id, AgentEventRecord.seq)
                > tuple_(after.ts, after.turn_id, after.seq)
            )
        stmt = (
            self._scoped(tenant_id)
            .where(*criteria)
            .order_by(AgentEventRecord.ts, AgentEventRecord.turn_id, AgentEventRecord.seq)
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
