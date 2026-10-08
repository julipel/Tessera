"""Публичный интерфейс модуля shared — единственная точка входа для других модулей."""

from app.modules.shared.api.admin import (
    AdminEditor,
    AdminViewer,
    CurrentAdmin,
    bearer_token,
    require_tenant_role,
)
from app.modules.shared.api.deps import DbSession, StreamDbSession, get_session
from app.modules.shared.api.errors import ApiError, install_error_handlers
from app.modules.shared.api.rate_limit import client_ip, enforce_rate_limits
from app.modules.shared.domain.admin import AdminAuthenticator, AdminPrincipal, AdminRole
from app.modules.shared.domain.errors import DomainError, NotFoundError, TenantMismatchError
from app.modules.shared.domain.ids import TenantId
from app.modules.shared.domain.rate_limit import RateLimit, RateLimiter
from app.modules.shared.infrastructure.db import (
    Base,
    TenantScopedBase,
    create_engine,
    create_session_factory,
)
from app.modules.shared.infrastructure.rate_limit import InMemoryRateLimiter, RedisRateLimiter
from app.modules.shared.infrastructure.repository import TenantRepository

__all__ = [
    "AdminAuthenticator",
    "AdminEditor",
    "AdminPrincipal",
    "AdminRole",
    "AdminViewer",
    "ApiError",
    "Base",
    "CurrentAdmin",
    "DbSession",
    "DomainError",
    "InMemoryRateLimiter",
    "NotFoundError",
    "RateLimit",
    "RateLimiter",
    "RedisRateLimiter",
    "StreamDbSession",
    "TenantId",
    "TenantMismatchError",
    "TenantRepository",
    "TenantScopedBase",
    "bearer_token",
    "client_ip",
    "create_engine",
    "create_session_factory",
    "enforce_rate_limits",
    "get_session",
    "install_error_handlers",
    "require_tenant_role",
]
