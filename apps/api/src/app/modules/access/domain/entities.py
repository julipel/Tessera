"""Пользователь админки и его роли в тенантах (ADR-0036)."""

from dataclasses import dataclass
from uuid import UUID

from app.modules.shared.kernel import AdminRole, TenantId

__all__ = ["AdminRole", "AdminUser", "TenantMembership"]


@dataclass(frozen=True, slots=True)
class AdminUser:
    """Суперадмин видит все тенанты и управляет пользователями; остальным доступны только
    тенанты, где у них есть роль. Отключённый пользователь не входит в админку."""

    id: UUID
    email: str
    is_superadmin: bool
    is_active: bool


@dataclass(frozen=True, slots=True)
class TenantMembership:
    tenant_id: TenantId
    role: AdminRole
