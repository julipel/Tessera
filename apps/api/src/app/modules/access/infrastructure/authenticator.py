"""Реализация порта `AdminAuthenticator` из shared (ADR-0036) — для зависимостей админки
в любом модуле."""

from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.modules.access.domain.sessions import hash_session_token
from app.modules.access.infrastructure.repositories import SqlAdminSessionStore
from app.modules.shared.public import AdminPrincipal


class SqlAdminAuthenticator:
    """Своя короткая сессия БД на проверку: соединение возвращается в пул до эндпоинта,
    и запрос не держит два соединения сразу (ADR-0033)."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self.session_factory = session_factory

    async def authenticate(self, token: str) -> AdminPrincipal | None:
        async with self.session_factory() as session:
            return await SqlAdminSessionStore(session).principal(
                hash_session_token(token), datetime.now(UTC)
            )
