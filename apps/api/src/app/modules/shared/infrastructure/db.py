"""Базовая декларативная модель, engine и фабрика сессий."""

from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import MetaData
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

# Явные имена constraint'ов — стабильные миграции и понятные downgrade.
NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)


class TenantScopedBase(Base):
    """Основа для бизнес-таблиц: UUID-ключ и обязательный tenant_id."""

    __abstract__ = True

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    tenant_id: Mapped[UUID] = mapped_column(index=True)


def create_engine(
    database_url: str,
    *,
    echo: bool = False,
    pool_size: int = 5,
    max_overflow: int = 10,
    pool_timeout: float = 30,
    statement_timeout_s: float | None = None,
) -> AsyncEngine:
    """`statement_timeout_s` — потолок одного запроса (REVIEW.md: S-4): зависший запрос не
    держит соединение пула бесконечно. Postgres прерывает его сам (`statement_timeout`),
    asyncpg — чуть позже на случай, если сервер не ответил вовсе (`command_timeout`)."""
    connect_args: dict[str, Any] = {}
    if statement_timeout_s is not None:
        connect_args = {
            "server_settings": {"statement_timeout": str(int(statement_timeout_s * 1000))},
            "command_timeout": statement_timeout_s + 5,
        }
    # hide_parameters: текст ошибок SQLAlchemy без значений запроса — в логи не попадают
    # контакты из заявок и формулировки пользователя (REVIEW.md: L-2, TO-5).
    return create_async_engine(
        database_url,
        echo=echo,
        hide_parameters=True,
        pool_pre_ping=True,
        pool_size=pool_size,
        max_overflow=max_overflow,
        pool_timeout=pool_timeout,
        connect_args=connect_args,
    )


def create_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    # expire_on_commit=False: объекты остаются читаемыми после commit без lazy-load в async.
    return async_sessionmaker(engine, expire_on_commit=False)
