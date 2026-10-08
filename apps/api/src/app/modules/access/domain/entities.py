"""Пользователь админки и его роли в тенантах (ADR-0036)."""

from dataclasses import dataclass
from enum import StrEnum
from uuid import UUID

from app.modules.shared.kernel import TenantId


class AdminRole(StrEnum):
    """Роль пользователя в тенанте. Старшая роль включает права младшей."""

    VIEWER = "viewer"  # диалоги, журнал хода
    EDITOR = "editor"  # + конфиг агента, источники знаний

    def allows(self, required: "AdminRole") -> bool:
        return _RANK[self] >= _RANK[required]


_RANK = {AdminRole.VIEWER: 0, AdminRole.EDITOR: 1}


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
