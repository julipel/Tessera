"""API админки: вход, выход, текущий пользователь (docs/contracts.md §7, ADR-0036)."""

from datetime import UTC, datetime, timedelta
from typing import Annotated

from fastapi import APIRouter, Header, Request, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.contracts import AdminLoginRequest, AdminLoginResponse, AdminMe, AdminMembership
from app.modules.access.application.auth import login, logout
from app.modules.access.domain.errors import InvalidCredentialsError
from app.modules.access.infrastructure.repositories import (
    SqlAdminSessionStore,
    SqlAdminUserDirectory,
)
from app.modules.shared.public import (
    AdminPrincipal,
    ApiError,
    CurrentAdmin,
    DbSession,
    RateLimit,
    RateLimiter,
    bearer_token,
    client_ip,
    enforce_rate_limits,
    ensure_not_limited,
)
from app.modules.tenants.public import SqlTenantDirectory

router = APIRouter(prefix="/v1/admin", tags=["admin"])


@router.post("/auth/login")
async def admin_login(
    body: AdminLoginRequest, request: Request, session: DbSession
) -> AdminLoginResponse:
    """Сессия по email и паролю; 401 без уточнения, что не так. Лимит по IP считает все
    попытки, по email — только неудачные (REVIEW.md: AC-3): перебор пароля одного
    пользователя с разных адресов упирается в лимит, а входы владельца его не тратят. Redis
    недоступен — лимиты считаются в памяти процесса, а не снимаются (ADR-0030)."""
    settings = request.app.state.settings
    limiter: RateLimiter = request.app.state.rate_limiter
    ip_rule = RateLimit(
        "admin_login_ip", settings.rate_admin_login_per_ip_per_min, 60, local_fallback=True
    )
    email_rule = RateLimit(
        "admin_login_email",
        settings.rate_admin_login_per_email_per_hour,
        3600,
        local_fallback=True,
    )
    email = body.email.strip().lower()
    await enforce_rate_limits(limiter, [(ip_rule, client_ip(request))])
    await ensure_not_limited(limiter, email_rule, email)
    try:
        result = await login(
            body.email,
            body.password,
            users=SqlAdminUserDirectory(session),
            sessions=SqlAdminSessionStore(session),
            ttl=timedelta(hours=settings.admin_session_ttl_hours),
            now=datetime.now(UTC),
        )
    except InvalidCredentialsError as e:
        await limiter.hit(email_rule, email)
        raise ApiError(status.HTTP_401_UNAUTHORIZED, "unauthorized", str(e)) from e
    return AdminLoginResponse(
        token=result.token,
        expires_at=result.expires_at,
        me=await _me(result.principal, session),
    )


@router.post("/auth/logout", status_code=status.HTTP_204_NO_CONTENT)
async def admin_logout(
    _: CurrentAdmin,
    session: DbSession,
    authorization: Annotated[str | None, Header()] = None,
) -> Response:
    token = bearer_token(authorization)
    assert token is not None  # CurrentAdmin пропускает только с токеном
    await logout(token, sessions=SqlAdminSessionStore(session))
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/me")
async def admin_me(principal: CurrentAdmin, session: DbSession) -> AdminMe:
    return await _me(principal, session)


async def _me(principal: AdminPrincipal, session: AsyncSession) -> AdminMe:
    tenants = SqlTenantDirectory(session)
    memberships = []
    for tenant_id, role in principal.roles.items():
        tenant = await tenants.get(tenant_id)
        if tenant is not None:  # роль удаляется каскадом вместе с тенантом
            memberships.append(
                AdminMembership(
                    tenant_id=tenant.id, tenant_slug=tenant.slug, tenant_name=tenant.name, role=role
                )
            )
    return AdminMe(
        id=principal.user_id,
        email=principal.email,
        is_superadmin=principal.is_superadmin,
        memberships=memberships,
    )
