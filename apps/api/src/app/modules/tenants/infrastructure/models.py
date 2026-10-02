"""ORM-модели модуля tenants."""

from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import DateTime, Enum, ForeignKey, Index, String, UniqueConstraint, func, text
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.modules.shared.public import Base, TenantScopedBase
from app.modules.tenants.domain.entities import AgentConfigStatus, TenantStatus


def _str_enum[E: (AgentConfigStatus, TenantStatus)](enum: type[E]) -> Enum:
    # VARCHAR + CHECK вместо нативного ENUM Postgres: новые значения без ALTER TYPE.
    return Enum(
        enum,
        name="status",
        native_enum=False,
        create_constraint=True,
        length=16,
        values_callable=lambda e: [m.value for m in e],
    )


class TenantRecord(Base):
    """Сам тенант — не TenantScopedBase: у него нет tenant_id."""

    __tablename__ = "tenants"

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    slug: Mapped[str] = mapped_column(String(64), unique=True)
    name: Mapped[str] = mapped_column(String(200))
    status: Mapped[TenantStatus] = mapped_column(
        _str_enum(TenantStatus), default=TenantStatus.ACTIVE
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class AgentConfigRecord(TenantScopedBase):
    __tablename__ = "agent_configs"
    __table_args__ = (
        UniqueConstraint("tenant_id", "version", name="uq_agent_configs_tenant_id_version"),
        # Не больше одной активной версии на тенанта — гарантирует БД.
        Index(
            "uq_agent_configs_one_active",
            "tenant_id",
            unique=True,
            postgresql_where=text("status = 'active'"),
        ),
    )

    tenant_id: Mapped[UUID] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), index=True
    )
    version: Mapped[int]
    status: Mapped[AgentConfigStatus] = mapped_column(_str_enum(AgentConfigStatus))
    config: Mapped[dict[str, Any]] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class WidgetKeyRecord(TenantScopedBase):
    __tablename__ = "widget_keys"

    tenant_id: Mapped[UUID] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), index=True
    )
    key_hash: Mapped[str] = mapped_column(String(64), unique=True)
    allowed_origins: Mapped[list[str]] = mapped_column(ARRAY(String), default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
