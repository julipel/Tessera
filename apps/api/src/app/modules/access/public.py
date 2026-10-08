"""Публичный интерфейс модуля access — единственная точка входа для других модулей."""

from app.modules.access.application.users import AdminUserResult, upsert_admin_user
from app.modules.access.domain.entities import AdminRole, AdminUser, TenantMembership
from app.modules.access.domain.errors import (
    InvalidEmailError,
    PasswordRequiredError,
    WeakPasswordError,
)
from app.modules.access.domain.passwords import MIN_PASSWORD_LENGTH, normalize_email
from app.modules.access.infrastructure.models import AdminMembershipRecord, AdminUserRecord
from app.modules.access.infrastructure.repositories import (
    AdminMembershipRepository,
    SqlAdminUserDirectory,
)

__all__ = [
    "MIN_PASSWORD_LENGTH",
    "AdminMembershipRecord",
    "AdminMembershipRepository",
    "AdminRole",
    "AdminUser",
    "AdminUserRecord",
    "AdminUserResult",
    "InvalidEmailError",
    "PasswordRequiredError",
    "SqlAdminUserDirectory",
    "TenantMembership",
    "WeakPasswordError",
    "normalize_email",
    "upsert_admin_user",
]
