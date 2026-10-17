"""ORM-модели модуля leads."""

from datetime import datetime
from uuid import UUID

from sqlalchemy import DateTime, ForeignKey, Index, Integer, String, Text, func, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.modules.shared.public import TenantScopedBase


class LeadRecord(TenantScopedBase):
    __tablename__ = "leads"
    __table_args__ = (
        # Список в админке и выгрузка — от новых к старым по (created_at, id) (ADR-0045).
        Index("ix_leads_tenant_id_created_at_id", "tenant_id", "created_at", "id"),
        Index("ix_leads_tenant_id_conversation_id", "tenant_id", "conversation_id"),
        Index("uq_leads_tenant_id_number", "tenant_id", "number", unique=True),
        # Без предиката: NULL у заявок до ключа не конфликтуют, ON CONFLICT выводит индекс.
        Index(
            "uq_leads_tenant_conversation_idempotency_key",
            "tenant_id",
            "conversation_id",
            "idempotency_key",
            unique=True,
        ),
    )

    # Индексы по tenant_id и conversation_id — составные выше (REVIEW.md: L-9).
    tenant_id: Mapped[UUID] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), index=False
    )
    # SET NULL: диалог удаляется по своему сроку хранения, заявка живёт свой (ADR-0043).
    conversation_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("conversations.id", ondelete="SET NULL")
    )
    # Номер заявки в тенанте из lead_counters (ADR-0045).
    number: Mapped[int] = mapped_column(Integer)
    form_key: Mapped[str] = mapped_column(String(64))
    fields: Mapped[dict[str, str]] = mapped_column(JSONB)
    # Заголовок формы и подписи полей на момент отправки; у заявок до 0020 — NULL и [].
    # Text: у заголовка формы в конфиге нет предела длины, а заявка уже подтверждена.
    form_title: Mapped[str | None] = mapped_column(Text)
    # Массив [{name, label}] в порядке формы: ключи JSONB-объекта Postgres переупорядочивает.
    field_labels: Mapped[list[dict[str, str]]] = mapped_column(
        JSONB, server_default=text("'[]'::jsonb")
    )
    # confirm_id подтверждения (P9-01g); у заявок, созданных раньше, — NULL.
    idempotency_key: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class LeadCounterRecord(TenantScopedBase):
    """Последний выданный номер заявки тенанта (ADR-0045); строка на тенант."""

    __tablename__ = "lead_counters"

    tenant_id: Mapped[UUID] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), unique=True, index=False
    )
    last_number: Mapped[int] = mapped_column(Integer)
