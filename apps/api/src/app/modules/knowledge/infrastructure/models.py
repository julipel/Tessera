"""ORM-модели модуля knowledge."""

from datetime import datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

from sqlalchemy import (
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.modules.knowledge.domain.entities import SourceKind, SourceStatus, SyncStatus
from app.modules.shared.public import TenantScopedBase


def _str_enum[E: (SourceKind, SourceStatus, SyncStatus)](enum: type[E], name: str) -> Enum:
    # VARCHAR + CHECK вместо нативного ENUM Postgres: новые значения без ALTER TYPE.
    return Enum(
        enum,
        name=name,
        native_enum=False,
        create_constraint=True,
        length=16,
        values_callable=lambda e: [m.value for m in e],
    )


def _tenant_fk() -> Mapped[UUID]:
    return mapped_column(ForeignKey("tenants.id", ondelete="CASCADE"), index=True)


def _source_fk() -> Mapped[UUID]:
    return mapped_column(ForeignKey("sources.id", ondelete="CASCADE"))


class SourceRecord(TenantScopedBase):
    __tablename__ = "sources"

    tenant_id: Mapped[UUID] = _tenant_fk()
    kind: Mapped[SourceKind] = mapped_column(_str_enum(SourceKind, "kind"))
    config: Mapped[dict[str, Any]] = mapped_column(
        JSONB, default=dict, server_default=text("'{}'::jsonb")
    )
    schedule: Mapped[str | None] = mapped_column(String(64))  # cron-выражение
    status: Mapped[SourceStatus] = mapped_column(
        _str_enum(SourceStatus, "status"), default=SourceStatus.ACTIVE
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class SourceSyncRecord(TenantScopedBase):
    __tablename__ = "source_syncs"
    __table_args__ = (Index("ix_source_syncs_source_id_started_at", "source_id", "started_at"),)

    tenant_id: Mapped[UUID] = _tenant_fk()
    source_id: Mapped[UUID] = _source_fk()
    status: Mapped[SyncStatus] = mapped_column(
        _str_enum(SyncStatus, "status"), default=SyncStatus.PENDING
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    stats: Mapped[dict[str, Any]] = mapped_column(
        JSONB, default=dict, server_default=text("'{}'::jsonb")
    )
    # Курсор инкрементальной синхронизации — непрозрачная строка коннектора.
    cursor: Mapped[str | None] = mapped_column(Text)
    error: Mapped[str | None] = mapped_column(Text)


class EntityRecord(TenantScopedBase):
    """Структурированная запись (товар, услуга, филиал). `type` задаёт конфиг источника."""

    __tablename__ = "entities"
    __table_args__ = (
        UniqueConstraint("source_id", "external_id", name="uq_entities_source_id_external_id"),
    )

    tenant_id: Mapped[UUID] = _tenant_fk()
    source_id: Mapped[UUID] = _source_fk()
    external_id: Mapped[str] = mapped_column(String(512))
    type: Mapped[str] = mapped_column(String(64))
    title: Mapped[str] = mapped_column(Text)
    price: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    currency: Mapped[str | None] = mapped_column(String(3))
    in_stock: Mapped[bool | None]
    category: Mapped[str | None] = mapped_column(String(256))
    url: Mapped[str | None] = mapped_column(Text)
    image_url: Mapped[str | None] = mapped_column(Text)
    attributes: Mapped[dict[str, Any]] = mapped_column(
        JSONB, default=dict, server_default=text("'{}'::jsonb")
    )
    content_hash: Mapped[str] = mapped_column(String(64))  # sha256 hex
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class DocumentRecord(TenantScopedBase):
    __tablename__ = "documents"
    __table_args__ = (
        UniqueConstraint("source_id", "external_id", name="uq_documents_source_id_external_id"),
    )

    tenant_id: Mapped[UUID] = _tenant_fk()
    source_id: Mapped[UUID] = _source_fk()
    # Текстовое описание Entity индексируется как документ со ссылкой на неё (architecture.md §8).
    entity_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("entities.id", ondelete="CASCADE"), index=True
    )
    external_id: Mapped[str] = mapped_column(String(512))
    title: Mapped[str] = mapped_column(Text, default="")
    url: Mapped[str | None] = mapped_column(Text)
    content_hash: Mapped[str] = mapped_column(String(64))  # sha256 hex
    metadata_: Mapped[dict[str, Any]] = mapped_column(
        "metadata", JSONB, default=dict, server_default=text("'{}'::jsonb")
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class ChunkRecord(TenantScopedBase):
    """`id` совпадает с id точки в Qdrant; вектор хранится только там."""

    __tablename__ = "chunks"
    __table_args__ = (UniqueConstraint("document_id", "ord", name="uq_chunks_document_id_ord"),)

    tenant_id: Mapped[UUID] = _tenant_fk()
    document_id: Mapped[UUID] = mapped_column(ForeignKey("documents.id", ondelete="CASCADE"))
    ord: Mapped[int] = mapped_column(Integer)
    section: Mapped[str | None] = mapped_column(Text)  # путь заголовков, если есть
    text: Mapped[str] = mapped_column(Text)
    token_count: Mapped[int] = mapped_column(Integer)
