"""ORM-модели модуля access."""

from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import DateTime, Enum, ForeignKey, String, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from app.modules.access.domain.entities import AdminRole
from app.modules.shared.public import Base, TenantScopedBase


class AdminUserRecord(Base):
    """Пользователь админки — не TenantScopedBase: тенантов у него может быть несколько
    (ADR-0036). Email хранится нормализованным (`normalize_email`)."""

    __tablename__ = "admin_users"

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    email: Mapped[str] = mapped_column(String(320), unique=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    is_superadmin: Mapped[bool] = mapped_column(default=False)
    is_active: Mapped[bool] = mapped_column(default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class AdminMembershipRecord(TenantScopedBase):
    """Роль пользователя в тенанте, не больше одной."""

    __tablename__ = "admin_memberships"
    __table_args__ = (
        UniqueConstraint("tenant_id", "user_id", name="uq_admin_memberships_tenant_id_user_id"),
    )

    tenant_id: Mapped[UUID] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), index=True
    )
    user_id: Mapped[UUID] = mapped_column(
        ForeignKey("admin_users.id", ondelete="CASCADE"), index=True
    )
    # VARCHAR + CHECK, как статусы в tenants: новые роли без ALTER TYPE.
    role: Mapped[AdminRole] = mapped_column(
        Enum(
            AdminRole,
            name="role",
            native_enum=False,
            create_constraint=True,
            length=16,
            values_callable=lambda e: [m.value for m in e],
        )
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class AdminSessionRecord(Base):
    """Сессия админки — не TenantScopedBase: принадлежит пользователю, а не тенанту
    (ADR-0036). Хранится только sha256 токена (`hash_session_token`)."""

    __tablename__ = "admin_sessions"

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    user_id: Mapped[UUID] = mapped_column(
        ForeignKey("admin_users.id", ondelete="CASCADE"), index=True
    )
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
