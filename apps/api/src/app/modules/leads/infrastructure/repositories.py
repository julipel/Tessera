"""Репозиторий заявок и хранилище для инструмента `create_lead` (порт tools `LeadStore`)."""

from collections.abc import Callable, Mapping
from datetime import datetime
from uuid import UUID

from sqlalchemy import ColumnElement, tuple_
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.leads.domain.lead import Lead, LeadCursor, LeadFilter
from app.modules.leads.infrastructure.models import LeadCounterRecord, LeadRecord
from app.modules.shared.public import TenantId, TenantRepository


def _lead(record: LeadRecord) -> Lead:
    return Lead(
        id=record.id,
        tenant_id=TenantId(record.tenant_id),
        conversation_id=record.conversation_id,
        number=record.number,
        form_key=record.form_key,
        fields=record.fields,
        created_at=record.created_at,
        form_title=record.form_title,
        field_labels={item["name"]: item["label"] for item in record.field_labels},
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
        form_title: str | None = None,
        field_labels: Mapping[str, str] | None = None,
    ) -> Lead:
        """Заявка с тем же `idempotency_key` в диалоге уже есть — возвращается она, новая не
        создаётся (P9-01g): ON CONFLICT, так что и параллельные вставки дают одну строку.
        Номер берётся из счётчика тенанта (ADR-0045); строка счётчика заблокирована до
        конца транзакции, поэтому номера у параллельных заявок разные."""
        if idempotency_key is not None:
            # Обычный повтор подтверждения номер не тратит; гонка двух вставок — тратит.
            existing = await self._by_key(tenant_id, conversation_id, idempotency_key)
            if existing is not None:
                return existing
        stmt = (
            insert(LeadRecord)
            .values(
                tenant_id=tenant_id,
                conversation_id=conversation_id,
                number=await self._next_number(tenant_id),
                form_key=form_key,
                fields=dict(fields),
                form_title=form_title,
                field_labels=[
                    {"name": name, "label": label} for name, label in (field_labels or {}).items()
                ],
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
        assert idempotency_key is not None  # без ключа конфликтовать нечему
        raced = await self._by_key(tenant_id, conversation_id, idempotency_key)
        assert raced is not None
        return raced

    async def _next_number(self, tenant_id: TenantId) -> int:
        c = LeadCounterRecord
        stmt = (
            insert(c)
            .values(tenant_id=tenant_id, last_number=1)
            .on_conflict_do_update(
                index_elements=[c.tenant_id], set_={"last_number": c.last_number + 1}
            )
            .returning(c.last_number)
        )
        return int((await self.session.execute(stmt)).scalar_one())

    async def _by_key(
        self, tenant_id: TenantId, conversation_id: UUID, idempotency_key: str
    ) -> Lead | None:
        found = await self.session.execute(
            self._scoped(tenant_id).where(
                LeadRecord.conversation_id == conversation_id,
                LeadRecord.idempotency_key == idempotency_key,
            )
        )
        record = found.scalar_one_or_none()
        return _lead(record) if record is not None else None

    async def list_for_conversation(self, tenant_id: TenantId, conversation_id: UUID) -> list[Lead]:
        stmt = (
            self._scoped(tenant_id)
            .where(LeadRecord.conversation_id == conversation_id)
            .order_by(LeadRecord.created_at, LeadRecord.id)
        )
        return [_lead(r) for r in (await self.session.scalars(stmt))]

    async def list_page(
        self,
        tenant_id: TenantId,
        *,
        before: LeadCursor | None,
        limit: int,
        where: LeadFilter,
    ) -> list[Lead]:
        r = LeadRecord
        criteria: list[ColumnElement[bool]] = []
        if before is not None:
            criteria.append(tuple_(r.created_at, r.id) < tuple_(before.created_at, before.id))
        if where.form_key is not None:
            criteria.append(r.form_key == where.form_key)
        if where.number is not None:
            criteria.append(r.number == where.number)
        if where.created_from is not None:
            criteria.append(r.created_at >= where.created_from)
        if where.created_to is not None:
            criteria.append(r.created_at < where.created_to)
        stmt = (
            self._scoped(tenant_id)
            .where(*criteria)
            .order_by(r.created_at.desc(), r.id.desc())
            .limit(limit)
        )
        return [_lead(rec) for rec in (await self.session.scalars(stmt))]

    async def purge_before(self, tenant_id: TenantId, before: datetime, limit: int) -> int:
        """Порция заявок, созданных раньше `before` (срок хранения, ADR-0043)."""
        return await self._delete_batch(tenant_id, LeadRecord.created_at < before, limit=limit)

    async def count_before(self, tenant_id: TenantId, before: datetime) -> int:
        return await self._count(tenant_id, LeadRecord.created_at < before)


class SqlLeadStore:
    """Каждая заявка — своя короткая сессия и commit: инструмент не делит `AsyncSession`
    стрима, а заявка не должна пропасть, если ход потом прервётся. Сбой БД — исключение
    (реестр инструментов превратит его в `upstream_error`). Повтор с тем же
    `idempotency_key` возвращает уже созданную заявку."""

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
        form_title: str,
        field_labels: Mapping[str, str],
    ) -> Lead:
        async with self._session_factory() as session:
            lead = await LeadRepository(session).create(
                tenant_id,
                conversation_id,
                form_key,
                fields,
                idempotency_key=idempotency_key,
                form_title=form_title,
                field_labels=field_labels,
            )
            await session.commit()
            return lead
