"""Репозитории модуля tenants (реализации портов из domain/ports.py)."""

from collections.abc import Collection, Sequence
from typing import Any
from uuid import UUID

from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.shared.public import TenantId, TenantRepository
from app.modules.tenants.domain.entities import (
    Activation,
    AgentConfigStatus,
    AgentConfigVersion,
    Tenant,
    TenantStatus,
    WidgetAccess,
)
from app.modules.tenants.domain.errors import AgentConfigNotFoundError
from app.modules.tenants.infrastructure.models import (
    AgentConfigRecord,
    TenantRecord,
    WidgetKeyRecord,
)


def _tenant(record: TenantRecord) -> Tenant:
    return Tenant(id=TenantId(record.id), slug=record.slug, name=record.name, status=record.status)


def _config(record: AgentConfigRecord) -> AgentConfigVersion:
    return AgentConfigVersion(
        id=record.id,
        tenant_id=TenantId(record.tenant_id),
        version=record.version,
        status=record.status,
        config=record.config,
        created_at=record.created_at,
    )


class SqlTenantDirectory:
    """Кросс-тенантный справочник (ADR-0006: отдельный явный сервис, не TenantRepository)."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get(self, tenant_id: TenantId) -> Tenant | None:
        record = await self.session.get(TenantRecord, tenant_id)
        return _tenant(record) if record else None

    async def get_many(self, tenant_ids: Collection[TenantId]) -> list[Tenant]:
        """Тенанты по id одним запросом, по slug; несуществующие id пропускаются."""
        if not tenant_ids:
            return []
        stmt = (
            select(TenantRecord)
            .where(TenantRecord.id.in_(list(tenant_ids)))
            .order_by(TenantRecord.slug)
        )
        return [_tenant(r) for r in (await self.session.execute(stmt)).scalars()]

    async def list_all(self) -> list[Tenant]:
        """Все тенанты по slug — для задач по всем тенантам (срок хранения, ADR-0043)."""
        records = await self.session.execute(select(TenantRecord).order_by(TenantRecord.slug))
        return [_tenant(r) for r in records.scalars()]

    async def get_by_slug(self, slug: str) -> Tenant | None:
        stmt = select(TenantRecord).where(TenantRecord.slug == slug)
        record = (await self.session.execute(stmt)).scalar_one_or_none()
        return _tenant(record) if record else None

    async def create(self, slug: str, name: str) -> Tenant:
        record = TenantRecord(slug=slug, name=name)
        self.session.add(record)
        await self.session.flush()
        await self.session.refresh(record)
        return _tenant(record)

    async def rename(self, tenant_id: TenantId, name: str) -> None:
        stmt = update(TenantRecord).where(TenantRecord.id == tenant_id).values(name=name)
        await self.session.execute(stmt)


class AgentConfigRepository(TenantRepository[AgentConfigRecord]):
    """`create_draft` и `activate` блокируют строку тенанта до конца транзакции: операции
    с версиями одного тенанта идут по очереди (REVIEW.md: T-2, T-3). В READ COMMITTED запрос
    после блокировки видит закоммиченное другой операцией — номер версии и прежняя активная
    версия берутся актуальные, уникальные индексы не срабатывают."""

    model = AgentConfigRecord

    async def _lock_tenant(self, tenant_id: TenantId) -> None:
        stmt = select(TenantRecord.id).where(TenantRecord.id == tenant_id).with_for_update()
        await self.session.execute(stmt)

    async def get_active(self, tenant_id: TenantId) -> AgentConfigVersion | None:
        stmt = self._scoped(tenant_id).where(AgentConfigRecord.status == AgentConfigStatus.ACTIVE)
        record = (await self.session.execute(stmt)).scalar_one_or_none()
        return _config(record) if record else None

    async def version_numbers(
        self, tenant_id: TenantId, config_ids: Collection[UUID]
    ) -> dict[UUID, int]:
        """Номера версий по id — для списков, где нужен только номер (диалоги в админке)."""
        if not config_ids:
            return {}
        stmt = select(AgentConfigRecord.id, AgentConfigRecord.version).where(
            AgentConfigRecord.tenant_id == tenant_id, AgentConfigRecord.id.in_(list(config_ids))
        )
        return {config_id: version for config_id, version in await self.session.execute(stmt)}

    async def get_version(self, tenant_id: TenantId, config_id: UUID) -> AgentConfigVersion | None:
        """Любая версия, в т.ч. архивная: диалог работает на той, с которой начат."""
        record = await self.get(tenant_id, config_id)
        return _config(record) if record else None

    async def list_versions(self, tenant_id: TenantId) -> Sequence[AgentConfigVersion]:
        """От старой версии к новой."""
        stmt = self._scoped(tenant_id).order_by(AgentConfigRecord.version)
        return [_config(r) for r in (await self.session.execute(stmt)).scalars()]

    async def create_draft(self, tenant_id: TenantId, config: dict[str, Any]) -> AgentConfigVersion:
        await self._lock_tenant(tenant_id)
        latest = select(func.coalesce(func.max(AgentConfigRecord.version), 0)).where(
            AgentConfigRecord.tenant_id == tenant_id
        )
        version = (await self.session.execute(latest)).scalar_one() + 1
        record = AgentConfigRecord(
            tenant_id=tenant_id, version=version, status=AgentConfigStatus.DRAFT, config=config
        )
        await self.add(tenant_id, record)
        await self.session.refresh(record)  # created_at — значение БД
        return _config(record)

    async def activate(self, tenant_id: TenantId, config_id: UUID) -> Activation:
        await self._lock_tenant(tenant_id)
        # populate_existing: статус версии мог смениться, пока ждали блокировку.
        stmt = (
            self._scoped(tenant_id)
            .where(AgentConfigRecord.id == config_id)
            .execution_options(populate_existing=True)
        )
        record = (await self.session.execute(stmt)).scalar_one_or_none()
        if record is None:
            raise AgentConfigNotFoundError(f"версия конфига {config_id} не найдена")
        if record.status == AgentConfigStatus.ACTIVE:
            return Activation(_config(record), previous_version=record.version)
        # Сначала архивируем прежнюю активную: частичный уникальный индекс проверяется сразу.
        archived = await self.session.execute(
            update(AgentConfigRecord)
            .where(
                AgentConfigRecord.tenant_id == tenant_id,
                AgentConfigRecord.status == AgentConfigStatus.ACTIVE,
            )
            .values(status=AgentConfigStatus.ARCHIVED)
            .returning(AgentConfigRecord.version)
        )
        previous = archived.scalar_one_or_none()
        record.status = AgentConfigStatus.ACTIVE
        await self.session.flush()
        return Activation(_config(record), previous_version=previous)


class WidgetKeyRepository(TenantRepository[WidgetKeyRecord]):
    model = WidgetKeyRecord

    async def has_any(self, tenant_id: TenantId) -> bool:
        stmt = select(self._scoped(tenant_id).exists())
        return bool((await self.session.execute(stmt)).scalar_one())

    async def has_key(self, tenant_id: TenantId, key_hash: str) -> bool:
        stmt = select(self._scoped(tenant_id).where(WidgetKeyRecord.key_hash == key_hash).exists())
        return bool((await self.session.execute(stmt)).scalar_one())

    async def delete_all(self, tenant_id: TenantId) -> int:
        stmt = (
            delete(WidgetKeyRecord)
            .where(WidgetKeyRecord.tenant_id == tenant_id)
            .returning(WidgetKeyRecord.id)
        )
        return len((await self.session.execute(stmt)).scalars().all())

    async def add_key(
        self, tenant_id: TenantId, key_hash: str, allowed_origins: Sequence[str]
    ) -> None:
        record = WidgetKeyRecord(
            tenant_id=tenant_id, key_hash=key_hash, allowed_origins=list(allowed_origins)
        )
        await self.add(tenant_id, record)


class SqlWidgetKeyResolver:
    """Кросс-тенантный поиск ключа по хэшу (ADR-0006, ADR-0007): единственное место, где
    widget_keys читается без tenant_id. Ключи отключённых тенантов не находятся."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def resolve(self, key_hash: str) -> WidgetAccess | None:
        stmt = (
            select(WidgetKeyRecord.tenant_id, WidgetKeyRecord.allowed_origins)
            .join(TenantRecord, TenantRecord.id == WidgetKeyRecord.tenant_id)
            .where(
                WidgetKeyRecord.key_hash == key_hash,
                TenantRecord.status == TenantStatus.ACTIVE,
            )
        )
        row = (await self.session.execute(stmt)).one_or_none()
        if row is None:
            return None
        return WidgetAccess(
            tenant_id=TenantId(row.tenant_id), allowed_origins=tuple(row.allowed_origins)
        )
