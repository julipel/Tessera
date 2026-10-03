"""Репозитории модуля tenants (реализации портов из domain/ports.py)."""

from collections.abc import Sequence
from typing import Any
from uuid import UUID

from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.shared.public import TenantId, TenantRepository
from app.modules.tenants.domain.entities import (
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
    )


class SqlTenantDirectory:
    """Кросс-тенантный справочник (ADR-0006: отдельный явный сервис, не TenantRepository)."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

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
    model = AgentConfigRecord

    async def get_active(self, tenant_id: TenantId) -> AgentConfigVersion | None:
        stmt = self._scoped(tenant_id).where(AgentConfigRecord.status == AgentConfigStatus.ACTIVE)
        record = (await self.session.execute(stmt)).scalar_one_or_none()
        return _config(record) if record else None

    async def get_version(self, tenant_id: TenantId, config_id: UUID) -> AgentConfigVersion | None:
        """Любая версия, в т.ч. архивная: диалог работает на той, с которой начат."""
        record = await self.get(tenant_id, config_id)
        return _config(record) if record else None

    async def list_versions(self, tenant_id: TenantId) -> Sequence[AgentConfigVersion]:
        stmt = self._scoped(tenant_id).order_by(AgentConfigRecord.version)
        return [_config(r) for r in (await self.session.execute(stmt)).scalars()]

    async def create_draft(self, tenant_id: TenantId, config: dict[str, Any]) -> AgentConfigVersion:
        latest = select(func.coalesce(func.max(AgentConfigRecord.version), 0)).where(
            AgentConfigRecord.tenant_id == tenant_id
        )
        version = (await self.session.execute(latest)).scalar_one() + 1
        record = AgentConfigRecord(
            tenant_id=tenant_id, version=version, status=AgentConfigStatus.DRAFT, config=config
        )
        await self.add(tenant_id, record)
        return _config(record)

    async def activate(self, tenant_id: TenantId, config_id: UUID) -> AgentConfigVersion:
        record = await self.get(tenant_id, config_id)
        if record is None:
            raise AgentConfigNotFoundError(f"AgentConfig {config_id} not found")
        if record.status != AgentConfigStatus.ACTIVE:
            # Сначала архивируем прежнюю активную: частичный уникальный индекс проверяется сразу.
            await self.session.execute(
                update(AgentConfigRecord)
                .where(
                    AgentConfigRecord.tenant_id == tenant_id,
                    AgentConfigRecord.status == AgentConfigStatus.ACTIVE,
                )
                .values(status=AgentConfigStatus.ARCHIVED)
            )
            record.status = AgentConfigStatus.ACTIVE
            await self.session.flush()
        return _config(record)


class WidgetKeyRepository(TenantRepository[WidgetKeyRecord]):
    model = WidgetKeyRecord

    async def has_any(self, tenant_id: TenantId) -> bool:
        stmt = select(self._scoped(tenant_id).exists())
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
