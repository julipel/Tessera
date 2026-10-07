"""Публичный интерфейс модуля shared — единственная точка входа для других модулей."""

from app.modules.shared.api.admin import AdminAccess
from app.modules.shared.api.deps import DbSession, StreamDbSession, get_session
from app.modules.shared.api.errors import ApiError, install_error_handlers
from app.modules.shared.domain.errors import DomainError, NotFoundError, TenantMismatchError
from app.modules.shared.domain.ids import TenantId
from app.modules.shared.infrastructure.db import (
    Base,
    TenantScopedBase,
    create_engine,
    create_session_factory,
)
from app.modules.shared.infrastructure.repository import TenantRepository

__all__ = [
    "AdminAccess",
    "ApiError",
    "Base",
    "DbSession",
    "DomainError",
    "NotFoundError",
    "StreamDbSession",
    "TenantId",
    "TenantMismatchError",
    "TenantRepository",
    "TenantScopedBase",
    "create_engine",
    "create_session_factory",
    "get_session",
    "install_error_handlers",
]
