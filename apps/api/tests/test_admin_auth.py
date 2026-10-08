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
    assert (await db_client.post(LOGOUT, headers=headers)).status_code == 401


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
    # Регистр и пробелы в email не обходят лимит; верный пароль тоже не пускает.
    limited = await db_client.post(LOGIN, json={"email": " ANNA@example.com", "password": PASSWORD})
    assert limited.status_code == 429
    assert limited.json()["error"]["code"] == "rate_limited"
    assert int(limited.headers["Retry-After"]) > 0

    # С того же IP — не больше 4 попыток в минуту на любые email.
    other = {"email": "bob@example.com", "password": "wrong password!"}
    assert (await db_client.post(LOGIN, json=other)).status_code == 401
    assert (await db_client.post(LOGIN, json=other)).status_code == 429
