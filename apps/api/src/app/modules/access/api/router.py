"""API админки: вход, выход, текущий пользователь (docs/contracts.md §7, ADR-0036).
Сессия браузера — HttpOnly-cookie, у CLI и тестов — Bearer (ADR-0044)."""

from datetime import UTC, datetime, timedelta
from typing import Annotated

from fastapi import APIRouter, Header, Request, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.contracts import AdminLoginRequest, AdminLoginResponse, AdminMe, AdminMembership
from app.modules.access.application.auth import login, logout
from app.modules.access.domain.errors import InvalidCredentialsError
from app.modules.access.infrastructure.kdf import KdfPool
from app.modules.access.infrastructure.repositories import (
    SqlAdminSessionStore,
    SqlAdminUserDirectory,
)
from app.modules.shared.public import (
    ADMIN_COOKIE,
    ADMIN_COOKIE_PATH,
    AdminPrincipal,
    ApiError,
    CurrentAdmin,
    DbSession,
    RateLimit,
    RateLimiter,
    admin_token,
    bearer_token,
    check_admin_origin,
    client_ip,
    enforce_rate_limits,
    ensure_not_limited,
)
from app.modules.tenants.public import SqlTenantDirectory

router = APIRouter(prefix="/v1/admin", tags=["admin"])


@router.post("/auth/login")
async def admin_login(
    body: AdminLoginRequest, request: Request, response: Response, session: DbSession
) -> AdminLoginResponse:
    """Сессия по email и паролю; 401 без уточнения, что не так. Лимит по IP считает все
    попытки, по email — только неудачные (REVIEW.md: AC-3): перебор пароля одного
    пользователя с разных адресов упирается в лимит, а входы владельца его не тратят. Redis
    недоступен — лимиты считаются в памяти процесса, а не снимаются (ADR-0030).

    Вход ставит cookie сессии (ADR-0044); с чужой страницы (Origin не из CORS_ORIGINS) —
    403: иначе сторонний сайт мог бы подсунуть браузеру свою сессию (login CSRF). Без Origin
    (CLI) — можно. Прежняя сессия из cookie закрывается."""
    settings = request.app.state.settings
    origin = request.headers.get("origin")
    if origin is not None and origin not in settings.cors_origins:
        raise ApiError(status.HTTP_403_FORBIDDEN, "forbidden", "вход не со страницы админки")
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
    now = datetime.now(UTC)
    sessions = SqlAdminSessionStore(session)
    kdf: KdfPool = request.app.state.kdf_pool
    try:
        result = await login(
            body.email,
            body.password,
            users=SqlAdminUserDirectory(session),
            sessions=sessions,
            verify=kdf.verify,
            # До login роутер в БД не пишет: commit фиксирует пустую транзакцию чтения и
            # возвращает соединение в пул. Запись в session выше этой строки зафиксировалась
            # бы и при неверном пароле.
            release=session.commit,
            ttl=timedelta(hours=settings.admin_session_ttl_hours),
            max_sessions=settings.admin_max_sessions_per_user,
            now=now,
        )
    except InvalidCredentialsError as e:
        await limiter.hit(email_rule, email)
        raise ApiError(status.HTTP_401_UNAUTHORIZED, "unauthorized", str(e)) from e
    if previous := request.cookies.get(ADMIN_COOKIE):
        await logout(previous, sessions=sessions)
    # Браузер держит токен в HttpOnly-cookie — скрипт страницы его не прочитает (ADR-0044);
    # в теле токен остаётся для Bearer-клиентов (CLI, тесты).
    response.set_cookie(
        ADMIN_COOKIE,
        result.token,
        max_age=int((result.expires_at - now).total_seconds()),
        path=ADMIN_COOKIE_PATH,
        secure=settings.admin_cookie_secure,
        httponly=True,
        samesite="strict",
    )
    return AdminLoginResponse(
        token=result.token,
        expires_at=result.expires_at,
        me=await _me(result.principal, session),
    )


@router.post("/auth/logout", status_code=status.HTTP_204_NO_CONTENT)
async def admin_logout(
    request: Request,
    session: DbSession,
    authorization: Annotated[str | None, Header()] = None,
) -> Response:
    """Закрывает сессию и стирает cookie (ADR-0044). Сессия уже истекла или закрыта —
    тоже 204: устаревшая cookie не должна оставаться в браузере. Нет токена — 401; запрос
    по cookie не со страницы админки — 403 (CSRF)."""
    token = admin_token(request, authorization)
    if token is None:
        raise ApiError(status.HTTP_401_UNAUTHORIZED, "unauthorized", "нужен вход в админку")
    if bearer_token(authorization) is None:
        check_admin_origin(request)
    await logout(token, sessions=SqlAdminSessionStore(session))
    response = Response(status_code=status.HTTP_204_NO_CONTENT)
    response.delete_cookie(
        ADMIN_COOKIE,
        path=ADMIN_COOKIE_PATH,
        secure=request.app.state.settings.admin_cookie_secure,
        httponly=True,
        samesite="strict",
    )
    return response


@router.get("/me")
async def admin_me(principal: CurrentAdmin, session: DbSession) -> AdminMe:
    return await _me(principal, session)


async def _me(principal: AdminPrincipal, session: AsyncSession) -> AdminMe:
    # Роль удаляется каскадом вместе с тенантом, так что тенант есть у каждой роли.
    tenants = await SqlTenantDirectory(session).get_many(principal.roles.keys())
    memberships = [
        AdminMembership(
            tenant_id=t.id, tenant_slug=t.slug, tenant_name=t.name, role=principal.roles[t.id]
        )
        for t in tenants
    ]
    return AdminMe(
        id=principal.user_id,
        email=principal.email,
        is_superadmin=principal.is_superadmin,
        memberships=memberships,
    )
