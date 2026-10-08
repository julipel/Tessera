"""Порты хранилищ модуля access (реализации — в infrastructure)."""

from collections.abc import Sequence
from datetime import datetime
from typing import Protocol
from uuid import UUID

from app.modules.access.domain.entities import AdminRole, AdminUser, TenantMembership
from app.modules.shared.kernel import AdminPrincipal, TenantId


class AdminUserDirectory(Protocol):
    """Пользователи админки. Не ограничен тенантом: пользователь может работать в нескольких
    тенантах (ADR-0036); его роли в тенантах — `AdminMembershipStore`."""

    async def get_by_email(self, email: str) -> AdminUser | None: ...

    async def get_credentials(self, email: str) -> tuple[AdminUser, str] | None:
        """Пользователь и хэш его пароля — только для входа."""
        ...

    async def create(self, email: str, password_hash: str, is_superadmin: bool) -> AdminUser: ...

    async def update(
        self,
        user_id: UUID,
        *,
        password_hash: str | None = None,
        is_superadmin: bool | None = None,
        is_active: bool | None = None,
    ) -> AdminUser:
        """Меняет только переданные поля."""
        ...

    async def memberships(self, user_id: UUID) -> Sequence[TenantMembership]:
        """Роли пользователя во всех тенантах."""
        ...


class AdminMembershipStore(Protocol):
    async def get_role(self, tenant_id: TenantId, user_id: UUID) -> AdminRole | None: ...

    async def set_role(self, tenant_id: TenantId, user_id: UUID, role: AdminRole) -> None: ...

    async def revoke(self, tenant_id: TenantId, user_id: UUID) -> bool: ...


class AdminSessionStore(Protocol):
    """Сессии админки: по хэшу токена (`hash_session_token`), сам токен не хранится."""

    async def create(self, user_id: UUID, token_hash: str, expires_at: datetime) -> None: ...

    async def principal(self, token_hash: str, now: datetime) -> AdminPrincipal | None:
        """Пользователь сессии с ролями; None — сессии нет, она истекла или пользователь
        отключён."""
        ...

    async def delete(self, token_hash: str) -> bool: ...

    async def delete_expired(self, user_id: UUID, now: datetime) -> int: ...
