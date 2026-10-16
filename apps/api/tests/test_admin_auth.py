"""Вход в админку, сессии и проверка роли в тенанте (P8-01b, ADR-0036)."""

from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.contracts import AdminLoginResponse, AdminMe
from app.modules.access.application import auth
from app.modules.access.domain.passwords import verify_password
from app.modules.access.domain.sessions import hash_session_token
from app.modules.access.public import (
    AdminMembershipRepository,
    AdminSessionRecord,
    SqlAdminSessionStore,
    SqlAdminUserDirectory,
    upsert_admin_user,
)
from app.modules.shared.public import AdminPrincipal, AdminRole, InMemoryRateLimiter, TenantId
from app.modules.tenants.public import SqlTenantDirectory

AdminLogin = Callable[..., Awaitable[dict[str, str]]]  # фикстура admin_login из conftest
LOGIN = "/v1/admin/auth/login"
LOGOUT = "/v1/admin/auth/logout"
ME = "/v1/admin/me"
PASSWORD = "correct horse battery"  # ADMIN_PASSWORD в conftest
INVALID = {
    "error": {"code": "unauthorized", "message": "неверный email или пароль", "retryable": False}
}


async def _tenant(session: AsyncSession, slug: str) -> TenantId:
    return (await SqlTenantDirectory(session).create(slug, f"Тенант {slug}")).id


async def _user(session: AsyncSession, email: str, **changes: Any) -> UUID:
    result = await upsert_admin_user(
        email,
        users=SqlAdminUserDirectory(session),
        memberships=AdminMembershipRepository(session),
        sessions=SqlAdminSessionStore(session),
        **changes,
    )
    return result.user.id


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


# --- Права ---


def test_superadmin_can_everything_and_roles_are_per_tenant() -> None:
    shop, other = TenantId(uuid4()), TenantId(uuid4())
    editor = AdminPrincipal(uuid4(), "e@x.io", is_superadmin=False, roles={shop: AdminRole.EDITOR})
    viewer = AdminPrincipal(uuid4(), "v@x.io", is_superadmin=False, roles={shop: AdminRole.VIEWER})
    root = AdminPrincipal(uuid4(), "r@x.io", is_superadmin=True)

    assert editor.can(shop, AdminRole.VIEWER) and editor.can(shop, AdminRole.EDITOR)
    assert viewer.can(shop, AdminRole.VIEWER) and not viewer.can(shop, AdminRole.EDITOR)
    assert not editor.can(other, AdminRole.VIEWER)
    assert root.can(other, AdminRole.EDITOR)


# --- Вход ---


async def test_login_issues_session_and_me_lists_roles(
    db_client: AsyncClient, db_session: AsyncSession
) -> None:
    shop = await _tenant(db_session, "shop")
    user_id = await _user(
        db_session, "Anna@Example.com", password=PASSWORD, grants={shop: AdminRole.EDITOR}
    )

    before = datetime.now(UTC)
    response = await db_client.post(
        LOGIN, json={"email": " anna@example.COM", "password": PASSWORD}
    )

    assert response.status_code == 200
    login = AdminLoginResponse.model_validate(response.json())
    assert login.token.startswith("as_")
    assert (
        before + timedelta(hours=12) <= login.expires_at <= datetime.now(UTC) + timedelta(hours=12)
    )
    expected = {
        "id": str(user_id),
        "email": "anna@example.com",
        "is_superadmin": False,
        "memberships": [
            {
                "tenant_id": str(shop),
                "tenant_slug": "shop",
                "tenant_name": "Тенант shop",
                "role": "editor",
            }
        ],
    }
    assert login.me.model_dump(mode="json") == expected

    me = await db_client.get(ME, headers=_bearer(login.token))
    assert me.status_code == 200
    assert AdminMe.model_validate(me.json()).model_dump(mode="json") == expected

    # В БД — только хэш токена.
    stored = (await db_session.execute(select(AdminSessionRecord))).scalars().all()
    assert [s.token_hash for s in stored] == [hash_session_token(login.token)]
    assert login.token not in stored[0].token_hash


@pytest.mark.parametrize(
    ("email", "password"),
    [
        ("anna@example.com", "wrong password!"),
        ("nobody@example.com", PASSWORD),
        ("not-an-email", PASSWORD),
        ("off@example.com", PASSWORD),
    ],
    ids=["wrong-password", "unknown-email", "malformed-email", "inactive-user"],
)
async def test_failed_login_is_indistinguishable(
    db_client: AsyncClient,
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    email: str,
    password: str,
) -> None:
    await _user(db_session, "anna@example.com", password=PASSWORD)
    await _user(db_session, "off@example.com", password=PASSWORD, is_active=False)
    checked: list[str] = []

    def counting_verify(given: str, encoded: str) -> bool:
        checked.append(encoded)
        return verify_password(given, encoded)

    monkeypatch.setattr(auth, "verify_password", counting_verify)

    response = await db_client.post(LOGIN, json={"email": email, "password": password})

    assert response.status_code == 401
    assert response.json() == INVALID
    # Пароль проверяется всегда — для неизвестного email по фиктивному хэшу.
    assert len(checked) == 1
    assert (await db_session.execute(select(AdminSessionRecord))).scalars().all() == []


# --- Сессия ---


async def test_logout_closes_session(db_client: AsyncClient, admin_login: AdminLogin) -> None:
    headers = await admin_login()

    assert (await db_client.post(LOGOUT, headers=headers)).status_code == 204
    assert (await db_client.get(ME, headers=headers)).status_code == 401
    # Повторный выход идемпотентен (ADR-0044: устаревшая cookie тоже стирается); без токена — 401.
    assert (await db_client.post(LOGOUT, headers=headers)).status_code == 204
    assert (await db_client.post(LOGOUT)).status_code == 401


@pytest.mark.parametrize(
    "headers",
    [{}, {"Authorization": "Bearer "}, {"Authorization": "Basic YWRtaW4="}, _bearer("as_nope")],
    ids=["none", "empty", "basic", "unknown"],
)
async def test_me_requires_session(db_client: AsyncClient, headers: dict[str, str]) -> None:
    response = await db_client.get(ME, headers=headers)

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "unauthorized"


async def test_expired_session_is_rejected_and_cleaned_on_login(
    db_client: AsyncClient, db_session: AsyncSession
) -> None:
    user_id = await _user(db_session, "anna@example.com", password=PASSWORD)
    db_session.add(
        AdminSessionRecord(
            user_id=user_id,
            token_hash=hash_session_token("as_old"),
            expires_at=datetime.now(UTC) - timedelta(seconds=1),
        )
    )
    await db_session.flush()

    assert (await db_client.get(ME, headers=_bearer("as_old"))).status_code == 401

    await db_client.post(LOGIN, json={"email": "anna@example.com", "password": PASSWORD})
    hashes = (await db_session.execute(select(AdminSessionRecord.token_hash))).scalars().all()
    assert hash_session_token("as_old") not in hashes
    assert len(hashes) == 1


async def test_disabling_user_or_revoking_role_applies_to_open_session(
    db_client: AsyncClient, db_session: AsyncSession, admin_login: AdminLogin
) -> None:
    shop = await _tenant(db_session, "shop")
    headers = await admin_login("anna@example.com", roles={shop: AdminRole.VIEWER})
    events = f"/v1/admin/tenants/{shop}/events?turn_id={uuid4()}"
    assert (await db_client.get(events, headers=headers)).status_code == 200

    await _user(db_session, "anna@example.com", revokes=[shop])
    assert (await db_client.get(events, headers=headers)).status_code == 403
    assert (await db_client.get(ME, headers=headers)).json()["memberships"] == []

    await _user(db_session, "anna@example.com", is_active=False)
    assert (await db_client.get(ME, headers=headers)).status_code == 401


@pytest.mark.parametrize(
    "change",
    [{"password": "another long secret"}, {"is_active": False}],
    ids=["password", "disabled"],
)
async def test_password_change_or_disabling_closes_open_sessions(
    db_client: AsyncClient, db_session: AsyncSession, admin_login: AdminLogin, change: Any
) -> None:
    anna = await admin_login("anna@example.com")
    bob = await admin_login("bob@example.com")

    await _user(db_session, "anna@example.com", **change)

    # Украденный токен не живёт до конца срока сессии (REVIEW.md: AC-1).
    assert (await db_client.get(ME, headers=anna)).status_code == 401
    assert (await db_client.get(ME, headers=bob)).status_code == 200
    user_id = await _user(db_session, "anna@example.com", is_active=True)
    sessions = select(AdminSessionRecord).where(AdminSessionRecord.user_id == user_id)
    assert (await db_session.execute(sessions)).scalars().all() == []


async def test_role_change_keeps_open_sessions(
    db_client: AsyncClient, db_session: AsyncSession, admin_login: AdminLogin
) -> None:
    shop = await _tenant(db_session, "shop")
    headers = await admin_login("anna@example.com")

    result = await upsert_admin_user(
        "anna@example.com",
        users=SqlAdminUserDirectory(db_session),
        memberships=AdminMembershipRepository(db_session),
        sessions=SqlAdminSessionStore(db_session),
        grants={shop: AdminRole.VIEWER},
    )

    assert result.closed_sessions == 0
    assert (await db_client.get(ME, headers=headers)).status_code == 200


# --- Лимит частоты ---


async def test_login_is_rate_limited_by_email_and_ip(
    app: FastAPI, db_client: AsyncClient, db_session: AsyncSession
) -> None:
    app.state.settings = app.state.settings.model_copy(
        update={"rate_admin_login_per_email_per_hour": 2, "rate_admin_login_per_ip_per_min": 4}
    )
    # Часы стоят в середине окна: на границе минуты счётчик IP обнулился бы посреди теста.
    app.state.rate_limiter = InMemoryRateLimiter(now=lambda: 1_800_030.0)
    await _user(db_session, "anna@example.com", password=PASSWORD)
    wrong = {"email": "anna@example.com", "password": "wrong password!"}

    assert [(await db_client.post(LOGIN, json=wrong)).status_code for _ in range(2)] == [401, 401]
    # Две неудачные попытки исчерпали лимит email: регистр и пробелы его не обходят, верный
    # пароль до конца окна тоже не пускает.
    limited = await db_client.post(LOGIN, json={"email": " ANNA@example.com", "password": PASSWORD})
    assert limited.status_code == 429
    assert limited.json()["error"]["code"] == "rate_limited"
    assert int(limited.headers["Retry-After"]) > 0

    # С того же IP — не больше 4 попыток в минуту на любые email.
    other = {"email": "bob@example.com", "password": "wrong password!"}
    assert (await db_client.post(LOGIN, json=other)).status_code == 401
    assert (await db_client.post(LOGIN, json=other)).status_code == 429


async def test_successful_logins_do_not_use_up_email_limit(
    app: FastAPI, db_client: AsyncClient, db_session: AsyncSession
) -> None:
    app.state.settings = app.state.settings.model_copy(
        update={"rate_admin_login_per_email_per_hour": 2, "rate_admin_login_per_ip_per_min": 100}
    )
    app.state.rate_limiter = InMemoryRateLimiter(now=lambda: 1_800_030.0)
    await _user(db_session, "anna@example.com", password=PASSWORD)
    right = {"email": "anna@example.com", "password": PASSWORD}
    wrong = {"email": "anna@example.com", "password": "wrong password!"}

    # Владелец входит сколько угодно раз: по email считаются только неудачи (REVIEW.md: AC-3).
    assert [(await db_client.post(LOGIN, json=right)).status_code for _ in range(3)] == [200] * 3
    assert (await db_client.post(LOGIN, json=wrong)).status_code == 401
    assert (await db_client.post(LOGIN, json=right)).status_code == 200
    assert (await db_client.post(LOGIN, json=wrong)).status_code == 401
    assert (await db_client.post(LOGIN, json=right)).status_code == 429


# --- Сессия в HttpOnly-cookie (ADR-0044) ---

ADMIN_ORIGIN = "http://localhost:3000"  # CORS_ORIGINS по умолчанию


async def _cookie_login(db_client: AsyncClient, db_session: AsyncSession) -> tuple[str, str]:
    """Вход; возвращает токен и заголовок Set-Cookie."""
    await _user(db_session, "anna@example.com", password=PASSWORD, is_superadmin=True)
    response = await db_client.post(LOGIN, json={"email": "anna@example.com", "password": PASSWORD})
    assert response.status_code == 200
    return response.json()["token"], response.headers["set-cookie"]


def _cookie(token: str) -> dict[str, str]:
    return {"Cookie": f"tessera_admin={token}"}


async def test_login_sets_httponly_strict_cookie(
    db_client: AsyncClient, db_session: AsyncSession
) -> None:
    token, set_cookie = await _cookie_login(db_client, db_session)

    attrs = [part.strip() for part in set_cookie.split(";")]
    assert attrs[0] == f"tessera_admin={token}"
    lowered = {a.lower() for a in attrs[1:]}
    assert lowered >= {"httponly", "secure", "samesite=strict", "path=/v1/admin"}
    # Срок — до expires_at сессии (12 ч), с точностью до секунды обработки.
    (max_age,) = [int(a.split("=")[1]) for a in lowered if a.startswith("max-age=")]
    assert 12 * 3600 - 5 <= max_age <= 12 * 3600


async def test_cookie_session_reads_and_writes_from_own_origin(
    db_client: AsyncClient, db_session: AsyncSession
) -> None:
    token, _ = await _cookie_login(db_client, db_session)

    me = await db_client.get(ME, headers=_cookie(token))
    assert me.status_code == 200
    assert me.json()["email"] == "anna@example.com"

    logout = await db_client.post(LOGOUT, headers=_cookie(token) | {"Origin": ADMIN_ORIGIN})
    assert logout.status_code == 204
    # Cookie стёрта, сессия закрыта.
    cleared = [part.strip().lower() for part in logout.headers["set-cookie"].split(";")]
    assert cleared[0] in ('tessera_admin=""', "tessera_admin=")
    assert "max-age=0" in cleared and "path=/v1/admin" in cleared
    assert (await db_client.get(ME, headers=_cookie(token))).status_code == 401


@pytest.mark.parametrize(
    "origin", [None, "http://evil.example", "http://localhost:3001"], ids=["none", "evil", "port"]
)
async def test_cookie_write_from_foreign_origin_is_forbidden(
    db_client: AsyncClient, db_session: AsyncSession, origin: str | None
) -> None:
    """CSRF: изменяющий запрос по cookie — только со страницы админки."""
    token, _ = await _cookie_login(db_client, db_session)
    headers = _cookie(token) | ({"Origin": origin} if origin else {})

    response = await db_client.post(LOGOUT, headers=headers)

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "forbidden"
    # Сессия не закрыта: запрос отклонён до действия.
    assert (await db_client.get(ME, headers=_cookie(token))).status_code == 200


async def test_bearer_write_needs_no_origin(
    db_client: AsyncClient, db_session: AsyncSession
) -> None:
    """Bearer браузер сам не подставит — CSRF ему не грозит, Origin не проверяется (CLI)."""
    token, _ = await _cookie_login(db_client, db_session)

    assert (await db_client.post(LOGOUT, headers=_bearer(token))).status_code == 204


async def test_insecure_cookie_setting_for_http_development(
    app: FastAPI, db_client: AsyncClient, db_session: AsyncSession
) -> None:
    app.state.settings = app.state.settings.model_copy(update={"admin_cookie_secure": False})

    _, set_cookie = await _cookie_login(db_client, db_session)

    assert "secure" not in {a.strip().lower() for a in set_cookie.split(";")}


async def test_bearer_takes_precedence_over_cookie(
    db_client: AsyncClient, db_session: AsyncSession
) -> None:
    token, _ = await _cookie_login(db_client, db_session)

    response = await db_client.get(ME, headers=_cookie(token) | _bearer("as_nope"))

    assert response.status_code == 401


async def test_cookie_write_to_tenant_endpoint_from_foreign_origin_is_forbidden(
    db_client: AsyncClient, db_session: AsyncSession
) -> None:
    """Проверка Origin — в общей зависимости: любой изменяющий путь админки, не только выход."""
    token, _ = await _cookie_login(db_client, db_session)
    path = f"/v1/admin/tenants/{uuid4()}/conversations/{uuid4()}/messages/{uuid4()}/rating"
    headers = _cookie(token) | {"Origin": "http://evil.example"}

    put = await db_client.put(path, headers=headers, json={"rating": "good"})
    delete = await db_client.delete(path, headers=headers)

    assert (put.status_code, delete.status_code) == (403, 403)
    assert put.json()["error"]["code"] == "forbidden"


@pytest.mark.parametrize("expired", [True, False], ids=["expired-session", "unknown-token"])
async def test_logout_with_stale_cookie_clears_it(
    db_client: AsyncClient, db_session: AsyncSession, expired: bool
) -> None:
    token = "as_unknown"
    if expired:
        token, _ = await _cookie_login(db_client, db_session)
        record = (await db_session.execute(select(AdminSessionRecord))).scalar_one()
        record.expires_at = datetime.now(UTC) - timedelta(minutes=1)
        await db_session.flush()
        assert (await db_client.get(ME, headers=_cookie(token))).status_code == 401

    response = await db_client.post(LOGOUT, headers=_cookie(token) | {"Origin": ADMIN_ORIGIN})

    assert response.status_code == 204
    assert "max-age=0" in response.headers["set-cookie"].lower()


async def test_login_from_foreign_page_is_forbidden(
    db_client: AsyncClient, db_session: AsyncSession
) -> None:
    """Login CSRF: чужая страница не подсунет браузеру свою сессию."""
    await _user(db_session, "anna@example.com", password=PASSWORD)

    response = await db_client.post(
        LOGIN,
        json={"email": "anna@example.com", "password": PASSWORD},
        headers={"Origin": "http://evil.example"},
    )

    assert response.status_code == 403
    assert "set-cookie" not in response.headers
    assert (await db_session.execute(select(AdminSessionRecord))).scalars().all() == []


async def test_relogin_closes_previous_cookie_session(
    db_client: AsyncClient, db_session: AsyncSession
) -> None:
    first, _ = await _cookie_login(db_client, db_session)

    again = await db_client.post(
        LOGIN,
        json={"email": "anna@example.com", "password": PASSWORD},
        headers=_cookie(first) | {"Origin": ADMIN_ORIGIN},
    )

    assert again.status_code == 200
    assert (await db_client.get(ME, headers=_bearer(first))).status_code == 401
    assert (await db_client.get(ME, headers=_bearer(again.json()["token"]))).status_code == 200
