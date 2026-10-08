"""Вход и выход пользователя админки (ADR-0036)."""

import asyncio
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta
from functools import cache

from app.modules.access.domain.errors import InvalidCredentialsError, InvalidEmailError
from app.modules.access.domain.passwords import hash_password, normalize_email, verify_password
from app.modules.access.domain.ports import AdminSessionStore, AdminUserDirectory
from app.modules.access.domain.sessions import generate_session_token, hash_session_token
from app.modules.shared.kernel import AdminPrincipal


@dataclass(frozen=True, slots=True)
class LoginResult:
    token: str  # показывается один раз, в БД — только хэш
    expires_at: datetime
    principal: AdminPrincipal


@cache
def _dummy_hash() -> str:
    """Хэш для неизвестного email: проверка пароля идёт всегда, и по времени ответа нельзя
    узнать, есть ли такой пользователь."""
    return hash_password(secrets.token_urlsafe(16))


async def login(
    email: str,
    password: str,
    *,
    users: AdminUserDirectory,
    sessions: AdminSessionStore,
    ttl: timedelta,
    now: datetime,
) -> LoginResult:
    """Новая сессия по email и паролю. Неверный email или пароль, отключённый пользователь —
    `InvalidCredentialsError` без уточнения причины."""
    try:
        credentials = await users.get_credentials(normalize_email(email))
    except InvalidEmailError:
        credentials = None
    password_hash = credentials[1] if credentials else await asyncio.to_thread(_dummy_hash)
    valid = await asyncio.to_thread(verify_password, password, password_hash)
    if credentials is None or not valid or not credentials[0].is_active:
        raise InvalidCredentialsError("неверный email или пароль")

    user = credentials[0]
    await sessions.delete_expired(user.id, now)
    token = generate_session_token()
    expires_at = now + ttl
    await sessions.create(user.id, hash_session_token(token), expires_at)
    principal = await sessions.principal(hash_session_token(token), now)
    assert principal is not None  # сессия только что создана, пользователь активен
    return LoginResult(token=token, expires_at=expires_at, principal=principal)


async def logout(token: str, *, sessions: AdminSessionStore) -> bool:
    """Закрыть сессию; False — её уже нет."""
    return await sessions.delete(hash_session_token(token))
