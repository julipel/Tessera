"""Репозиторий заявок и хранилище для инструмента `create_lead` (порт tools `LeadStore`)."""

from collections.abc import Callable, Mapping
from uuid import UUID

from sqlalchemy.dialects.postgresql import insert
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
        *,
        idempotency_key: str | None = None,
    ) -> Lead:
        """Заявка с тем же `idempotency_key` в диалоге уже есть — возвращается она, новая не
        создаётся (P9-01g): ON CONFLICT, так что и параллельные вставки дают одну строку."""
        stmt = (
            insert(LeadRecord)
            .values(
                tenant_id=tenant_id,
                conversation_id=conversation_id,
                form_key=form_key,
                fields=dict(fields),
                idempotency_key=idempotency_key,
            )
            .on_conflict_do_nothing(
                index_elements=["tenant_id", "conversation_id", "idempotency_key"]
            )
            .returning(LeadRecord)
        )
        created = (await self.session.execute(stmt)).scalar_one_or_none()
        if created is not None:
            return _lead(created)
        existing = await self.session.execute(
            self._scoped(tenant_id).where(
                LeadRecord.conversation_id == conversation_id,
                LeadRecord.idempotency_key == idempotency_key,
            )
        )
        return _lead(existing.scalar_one())

    async def list_for_conversation(self, tenant_id: TenantId, conversation_id: UUID) -> list[Lead]:
        records = await self.list(tenant_id, LeadRecord.conversation_id == conversation_id)
        return [_lead(r) for r in sorted(records, key=lambda r: r.created_at)]


class SqlLeadStore:
    """Каждая заявка — своя короткая сессия и commit: инструмент не делит `AsyncSession`
    стрима, а заявка не должна пропасть, если ход потом прервётся. Сбой БД — исключение
    (реестр инструментов превратит его в `upstream_error`). Повтор с тем же
    `idempotency_key` возвращает id уже созданной заявки."""

    def __init__(self, session_factory: Callable[[], AsyncSession]) -> None:
        self._session_factory = session_factory

    async def create(
        self,
        tenant_id: TenantId,
        conversation_id: UUID,
        form_key: str,
        fields: Mapping[str, str],
        *,
        idempotency_key: str,
    ) -> UUID:
        async with self._session_factory() as session:
            lead = await LeadRepository(session).create(
                tenant_id, conversation_id, form_key, fields, idempotency_key=idempotency_key
            )
            await session.commit()
            return lead.id
