"""Репозиторий заявок и хранилище для инструмента `create_lead` (порт tools `LeadStore`)."""

from collections.abc import Callable, Mapping
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.leads.domain.lead import Lead
from app.modules.leads.infrastructure.models import LeadRecord
from app.modules.shared.public import TenantId, TenantRepository


def _lead(record: LeadRecord) -> Lead:
    return Lead(
        id=record.id,
        tenant_id=TenantId(record.tenant_id),
        conversation_id=record.conversation_id,
        form_key=record.form_key,
        fields=record.fields,
        created_at=record.created_at,
    )


class LeadRepository(TenantRepository[LeadRecord]):
    model = LeadRecord

    async def create(
        self,
        tenant_id: TenantId,
        conversation_id: UUID,
        form_key: str,
        fields: Mapping[str, str],
    ) -> Lead:
        record = LeadRecord(
            tenant_id=tenant_id,
            conversation_id=conversation_id,
            form_key=form_key,
            fields=dict(fields),
        )
        await self.add(tenant_id, record)
        await self.session.refresh(record)  # created_at задаёт БД
        return _lead(record)

    async def list_for_conversation(self, tenant_id: TenantId, conversation_id: UUID) -> list[Lead]:
        records = await self.list(tenant_id, LeadRecord.conversation_id == conversation_id)
        return [_lead(r) for r in sorted(records, key=lambda r: r.created_at)]


class SqlLeadStore:
    """Каждая заявка — своя короткая сессия и commit: инструмент не делит `AsyncSession`
    стрима, а заявка не должна пропасть, если ход потом прервётся. Сбой БД — исключение
    (реестр инструментов превратит его в `upstream_error`)."""

    def __init__(self, session_factory: Callable[[], AsyncSession]) -> None:
        self._session_factory = session_factory

    async def create(
        self,
        tenant_id: TenantId,
        conversation_id: UUID,
        form_key: str,
        fields: Mapping[str, str],
    ) -> UUID:
        async with self._session_factory() as session:
            lead = await LeadRepository(session).create(
                tenant_id, conversation_id, form_key, fields
            )
            await session.commit()
            return lead.id
