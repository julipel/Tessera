"""ORM-модели модуля leads."""

from datetime import datetime
from uuid import UUID

from sqlalchemy import DateTime, ForeignKey, Index, String, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.modules.shared.public import TenantScopedBase


class LeadRecord(TenantScopedBase):
    __tablename__ = "leads"
    __table_args__ = (
        Index("ix_leads_tenant_id_created_at", "tenant_id", "created_at"),
        # Без предиката: NULL у заявок до ключа не конфликтуют, ON CONFLICT выводит индекс.
        Index(
            "uq_leads_tenant_conversation_idempotency_key",
            "tenant_id",
            "conversation_id",
            "idempotency_key",
            unique=True,
        ),
    )

    tenant_id: Mapped[UUID] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), index=True
    )
    conversation_id: Mapped[UUID] = mapped_column(
        ForeignKey("conversations.id", ondelete="CASCADE"), index=True
    )
    form_key: Mapped[str] = mapped_column(String(64))
    fields: Mapped[dict[str, str]] = mapped_column(JSONB)
    # confirm_id подтверждения (P9-01g); у заявок, созданных раньше, — NULL.
    idempotency_key: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
