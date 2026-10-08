"""Доступ к админке из любого модуля (ADR-0036): кто вошёл и какая у него роль в тенанте.

Пользователей и сессии ведёт модуль `access`; здесь — только порт и то, что видят
эндпоинты админки других модулей, которым `access` недоступен.
"""

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Protocol
from uuid import UUID

from app.modules.shared.domain.ids import TenantId


class AdminRole(StrEnum):
    """Роль пользователя в тенанте. Старшая роль включает права младшей."""

    VIEWER = "viewer"  # диалоги, журнал хода
    EDITOR = "editor"  # + конфиг агента, источники знаний

    def allows(self, required: "AdminRole") -> bool:
        return _RANK[self] >= _RANK[required]


_RANK = {AdminRole.VIEWER: 0, AdminRole.EDITOR: 1}


@dataclass(frozen=True, slots=True)
class AdminPrincipal:
    """Пользователь админки с действующей сессией. Суперадмину доступны все тенанты."""

    user_id: UUID
    email: str
    is_superadmin: bool
    roles: Mapping[TenantId, AdminRole] = field(default_factory=dict)

    def can(self, tenant_id: TenantId, required: AdminRole) -> bool:
        if self.is_superadmin:
            return True
        role = self.roles.get(tenant_id)
        return role is not None and role.allows(required)


class AdminAuthenticator(Protocol):
    async def authenticate(self, token: str) -> AdminPrincipal | None:
        """Пользователь по токену сессии; None — токен неизвестен, сессия истекла или
        пользователь отключён."""
        ...
