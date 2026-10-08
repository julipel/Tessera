"""Репозитории модуля access (реализации портов из domain/ports.py)."""

from collections.abc import Sequence
from uuid import UUID

from sqlalchemy import ColumnElement, delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.access.domain.entities import AdminRole, AdminUser, TenantMembership
from app.modules.access.infrastructure.models import AdminMembershipRecord, AdminUserRecord
from app.modules.shared.public import NotFoundError, TenantId, TenantRepository


def _user(record: AdminUserRecord) -> AdminUser:
    return AdminUser(
        id=record.id,
        email=record.email,
        is_superadmin=record.is_superadmin,
        is_active=record.is_active,
    )


class SqlAdminUserDirectory:
    """Кросс-тенантный справочник пользователей админки (ADR-0036, по образцу
    SqlTenantDirectory, ADR-0007): пользователь не принадлежит одному тенанту."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get_by_email(self, email: str) -> AdminUser | None:
        record = await self._by(AdminUserRecord.email == email)
        return _user(record) if record else None

    async def create(self, email: str, password_hash: str, is_superadmin: bool) -> AdminUser:
        record = AdminUserRecord(
            email=email, password_hash=password_hash, is_superadmin=is_superadmin, is_active=True
        )
        self.session.add(record)
        await self.session.flush()
        return _user(record)

    async def update(
        self,
        user_id: UUID,
        *,
        password_hash: str | None = None,
        is_superadmin: bool | None = None,
        is_active: bool | None = None,
    ) -> AdminUser:
        record = await self._by(AdminUserRecord.id == user_id)
        if record is None:
            raise NotFoundError(f"AdminUser {user_id} not found")
        if password_hash is not None:
            record.password_hash = password_hash
        if is_superadmin is not None:
            record.is_superadmin = is_superadmin
        if is_active is not None:
            record.is_active = is_active
        await self.session.flush()
        return _user(record)

    async def memberships(self, user_id: UUID) -> Sequence[TenantMembership]:
        stmt = (
            select(AdminMembershipRecord.tenant_id, AdminMembershipRecord.role)
            .where(AdminMembershipRecord.user_id == user_id)
            .order_by(AdminMembershipRecord.created_at, AdminMembershipRecord.tenant_id)
        )
        rows = (await self.session.execute(stmt)).all()
        return [TenantMembership(tenant_id=TenantId(r.tenant_id), role=r.role) for r in rows]

    async def _by(self, criterion: ColumnElement[bool]) -> AdminUserRecord | None:
        stmt = select(AdminUserRecord).where(criterion)
        return (await self.session.execute(stmt)).scalar_one_or_none()


class AdminMembershipRepository(TenantRepository[AdminMembershipRecord]):
    model = AdminMembershipRecord

    async def get_role(self, tenant_id: TenantId, user_id: UUID) -> AdminRole | None:
        record = await self._get(tenant_id, user_id)
        return record.role if record else None

    async def set_role(self, tenant_id: TenantId, user_id: UUID, role: AdminRole) -> None:
        record = await self._get(tenant_id, user_id)
        if record is None:
            await self.add(
                tenant_id, AdminMembershipRecord(tenant_id=tenant_id, user_id=user_id, role=role)
            )
        elif record.role != role:
            record.role = role
            await self.session.flush()

    async def revoke(self, tenant_id: TenantId, user_id: UUID) -> bool:
        stmt = (
            delete(AdminMembershipRecord)
            .where(
                AdminMembershipRecord.tenant_id == tenant_id,
                AdminMembershipRecord.user_id == user_id,
            )
            .returning(AdminMembershipRecord.id)
        )
        return bool((await self.session.execute(stmt)).scalars().all())

    async def _get(self, tenant_id: TenantId, user_id: UUID) -> AdminMembershipRecord | None:
        stmt = self._scoped(tenant_id).where(AdminMembershipRecord.user_id == user_id)
        return (await self.session.execute(stmt)).scalar_one_or_none()
