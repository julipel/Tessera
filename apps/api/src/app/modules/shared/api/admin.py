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

ADMIN_COOKIE = "tessera_admin"
# Cookie уходит только в API админки (вход, выход и всё под /v1/admin).
ADMIN_COOKIE_PATH = "/v1/admin"
_SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})


def bearer_token(authorization: str | None) -> str | None:
    """Токен из `Authorization: Bearer <token>`; другой схемы или пустого токена — None."""
    scheme, _, token = (authorization or "").partition(" ")
    token = token.strip()
    return token if scheme.lower() == "bearer" and token else None


def admin_token(request: Request, authorization: str | None) -> str | None:
    """Токен сессии: `Authorization: Bearer` (CLI, тесты) или HttpOnly-cookie (браузер,
    ADR-0044)."""
    return bearer_token(authorization) or request.cookies.get(ADMIN_COOKIE) or None


def check_admin_origin(request: Request) -> None:
    """CSRF (ADR-0044): изменяющий запрос по cookie — только со своего сайта. SameSite=Strict
    не пускает cookie с чужих сайтов; Origin закрывает и соседние origin того же сайта.
    Браузер всегда шлёт Origin у fetch с POST/PUT/DELETE; нет заголовка — отказ."""
    if request.method in _SAFE_METHODS:
        return
    origin = request.headers.get("origin")
    if origin is None or origin not in request.app.state.settings.cors_origins:
        raise ApiError(status.HTTP_403_FORBIDDEN, "forbidden", "запрос не со страницы админки")


async def require_admin(
    request: Request, authorization: Annotated[str | None, Header()] = None
) -> AdminPrincipal:
    """401, если нет токена сессии (Bearer или cookie), сессия неизвестна или истекла,
    пользователь отключён. Запрос по cookie, меняющий данные, — 403 без своего Origin."""
    bearer = bearer_token(authorization)
    token = bearer or request.cookies.get(ADMIN_COOKIE) or None
    authenticator: AdminAuthenticator = request.app.state.admin_authenticator
    principal = await authenticator.authenticate(token) if token else None
    if principal is None:
        raise ApiError(status.HTTP_401_UNAUTHORIZED, "unauthorized", "нужен вход в админку")
    if bearer is None:
        check_admin_origin(request)
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
