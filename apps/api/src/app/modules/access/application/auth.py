"""Вход и выход пользователя админки (ADR-0036)."""

import hashlib
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Literal, NoReturn

import structlog

from app.modules.access.domain.errors import InvalidCredentialsError, InvalidEmailError
from app.modules.access.domain.passwords import dummy_hash, normalize_email
from app.modules.access.domain.ports import AdminSessionStore, AdminUserDirectory
from app.modules.access.domain.sessions import generate_session_token, hash_session_token
from app.modules.shared.kernel import AdminPrincipal

logger = structlog.get_logger(__name__)

# Проверка пароля (KDF) вне event loop: `KdfPool.verify`.
PasswordCheck = Callable[[str, str], Awaitable[bool]]


@dataclass(frozen=True, slots=True)
class LoginResult:
    token: str  # показывается один раз, в БД — только хэш
    expires_at: datetime
    principal: AdminPrincipal


async def login(
    email: str,
    password: str,
    *,
    users: AdminUserDirectory,
    sessions: AdminSessionStore,
    verify: PasswordCheck,
    release: Callable[[], Awaitable[None]],
    ttl: timedelta,
    max_sessions: int,
    now: datetime,
) -> LoginResult:
    """Новая сессия по email и паролю. Неверный email или пароль, отключённый пользователь —
    `InvalidCredentialsError` без уточнения причины (причина — только в логе).

    `release` закрывает транзакцию чтения до проверки пароля: ~0,1 с KDF на попытку не держат
    соединение пула БД (REVIEW.md: AC-4). Пароль проверяется всегда — для неизвестного email
    по фиктивному хэшу, и по времени ответа нельзя узнать, есть ли такой пользователь.
    Сессий у пользователя остаётся не больше `max_sessions` — самые старые закрываются."""
    try:
        credentials = await users.get_credentials(normalize_email(email))
    except InvalidEmailError:
        credentials = None
    await release()
    valid = await verify(password, credentials[1] if credentials else dummy_hash())
    if credentials is None:
        _fail(email, "unknown_user")
    user = credentials[0]
    if not valid:
        _fail(email, "bad_password")
    if not user.is_active:
        _fail(email, "inactive")

    await sessions.delete_expired(user.id, now)
    token = generate_session_token()
    expires_at = now + ttl
    await sessions.create(user.id, hash_session_token(token), expires_at)
    closed = await sessions.keep_latest(user.id, max_sessions, hash_session_token(token))
    principal = await sessions.principal(hash_session_token(token), now)
    if principal is None:  # пользователя отключили между проверкой пароля и созданием сессии
        _fail(email, "inactive")
    logger.info("admin.login_succeeded", user_id=str(user.id), sessions_closed=closed)
    return LoginResult(token=token, expires_at=expires_at, principal=principal)


async def logout(token: str, *, sessions: AdminSessionStore) -> bool:
    """Закрыть сессию; False — её уже нет."""
    return await sessions.delete(hash_session_token(token))


def _fail(email: str, reason: Literal["unknown_user", "bad_password", "inactive"]) -> NoReturn:
    """Лог отказа без email открытым текстом: по `email_hash` видно перебор одного аккаунта.
    Это псевдоним, а не анонимизация: хэш без соли, по списку известных адресов он
    восстанавливается."""
    digest = hashlib.sha256(email.strip().lower().encode()).hexdigest()[:16]
    logger.warning("admin.login_failed", reason=reason, email_hash=digest)
    raise InvalidCredentialsError("неверный email или пароль")
