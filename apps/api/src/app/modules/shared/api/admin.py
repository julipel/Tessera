"""Доступ к API админки по сессии пользователя и его роли в тенанте (ADR-0036).

Эндпоинты админки любого модуля объявляют нужную роль зависимостью отсюда и не
импортируют `access`: проверку сессии даёт `app.state.admin_authenticator` (main.py).
"""

from collections.abc import Awaitable, Callable
from typing import Annotated
from uuid import UUID

import structlog
from fastapi import Depends, Header, Request, status

from app.modules.shared.api.errors import ApiError
from app.modules.shared.domain.admin import AdminAuthenticator, AdminPrincipal, AdminRole
from app.modules.shared.domain.ids import TenantId


def bearer_token(authorization: str | None) -> str | None:
    """Токен из `Authorization: Bearer <token>`; другой схемы или пустого токена — None."""
    scheme, _, token = (authorization or "").partition(" ")
    token = token.strip()
    return token if scheme.lower() == "bearer" and token else None


async def require_admin(
    request: Request, authorization: Annotated[str | None, Header()] = None
) -> AdminPrincipal:
    """401, если нет `Authorization: Bearer <токен сессии>`, сессия неизвестна или истекла,
    пользователь отключён."""
    token = bearer_token(authorization)
    authenticator: AdminAuthenticator = request.app.state.admin_authenticator
    principal = await authenticator.authenticate(token) if token else None
    if principal is None:
        raise ApiError(status.HTTP_401_UNAUTHORIZED, "unauthorized", "нужен вход в админку")
    structlog.contextvars.bind_contextvars(admin_user_id=str(principal.user_id))
    return principal


CurrentAdmin = Annotated[AdminPrincipal, Depends(require_admin)]


def require_tenant_role(role: AdminRole) -> Callable[..., Awaitable[AdminPrincipal]]:
    """Зависимость для эндпоинтов `…/tenants/{tenant_id}/…`: 403, если у пользователя нет
    роли `role` (или старшей) в тенанте из пути. Суперадмину доступны все тенанты."""

    async def dependency(tenant_id: UUID, principal: CurrentAdmin) -> AdminPrincipal:
        if not principal.can(TenantId(tenant_id), role):
            raise ApiError(status.HTTP_403_FORBIDDEN, "forbidden", "нет доступа к тенанту")
        structlog.contextvars.bind_contextvars(tenant_id=str(tenant_id))
        return principal

    return dependency


AdminViewer = Depends(require_tenant_role(AdminRole.VIEWER))
AdminEditor = Depends(require_tenant_role(AdminRole.EDITOR))
