"""knowledge: источники, синхронизации, документы, чанки, сущности

Revision ID: 0005
Revises: 0004
Create Date: 2026-10-03
"""

from collections.abc import Sequence
from datetime import datetime

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql
from sqlalchemy.schema import SchemaItem

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SOURCE_KINDS = ("website", "file", "table", "http_api", "database")
SOURCE_STATUSES = ("active", "paused")
SYNC_STATUSES = ("pending", "running", "succeeded", "failed")


def _enum(column: str, *values: str) -> sa.Enum:
    # CHECK задаётся явно в таблице — у типа create_constraint не включаем, иначе дубль.
    return sa.Enum(*values, name=column, native_enum=False, length=16)


def _check(table: str, column: str, *values: str) -> sa.CheckConstraint:
    allowed = ", ".join(f"'{v}'" for v in values)
    return sa.CheckConstraint(f"{column} IN ({allowed})", name=op.f(f"ck_{table}_{column}"))


def _jsonb(name: str) -> sa.Column[dict[str, object]]:
    return sa.Column(
        name,
        postgresql.JSONB(astext_type=sa.Text()),
        server_default=sa.text("'{}'::jsonb"),
        nullable=False,
    )


def _now(name: str) -> sa.Column[datetime]:
    return sa.Column(
        name, sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
    )


def _fk(table: str, column: str, target: str) -> sa.ForeignKeyConstraint:
    return sa.ForeignKeyConstraint(
        [column], [f"{target}.id"], name=op.f(f"fk_{table}_{column}_{target}"), ondelete="CASCADE"
    )


def _base(table: str) -> list[SchemaItem]:
    """id, tenant_id и их ограничения — общие для всех таблиц модуля."""
    return [
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f(f"pk_{table}")),
        _fk(table, "tenant_id", "tenants"),
    ]


def _tenant_index(table: str) -> None:
    op.create_index(op.f(f"ix_{table}_tenant_id"), table, ["tenant_id"])


def upgrade() -> None:
    op.create_table(
        "sources",
        *_base("sources"),
        sa.Column("kind", _enum("kind", *SOURCE_KINDS), nullable=False),
        _jsonb("config"),
        sa.Column("schedule", sa.String(length=64), nullable=True),
        sa.Column("status", _enum("status", *SOURCE_STATUSES), nullable=False),
        _now("created_at"),
        _check("sources", "kind", *SOURCE_KINDS),
        _check("sources", "status", *SOURCE_STATUSES),
    )
    _tenant_index("sources")

    op.create_table(
        "source_syncs",
        *_base("source_syncs"),
        sa.Column("source_id", sa.Uuid(), nullable=False),
        sa.Column("status", _enum("status", *SYNC_STATUSES), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        _jsonb("stats"),
        sa.Column("cursor", sa.Text(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        _check("source_syncs", "status", *SYNC_STATUSES),
        _fk("source_syncs", "source_id", "sources"),
    )
    _tenant_index("source_syncs")
    op.create_index(
        "ix_source_syncs_source_id_started_at", "source_syncs", ["source_id", "started_at"]
    )

    op.create_table(
        "entities",
        *_base("entities"),
        sa.Column("source_id", sa.Uuid(), nullable=False),
        sa.Column("external_id", sa.String(length=512), nullable=False),
        sa.Column("type", sa.String(length=64), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("price", sa.Numeric(precision=12, scale=2), nullable=True),
        sa.Column("currency", sa.String(length=3), nullable=True),
        sa.Column("in_stock", sa.Boolean(), nullable=True),
        sa.Column("category", sa.String(length=256), nullable=True),
        sa.Column("url", sa.Text(), nullable=True),
        sa.Column("image_url", sa.Text(), nullable=True),
        _jsonb("attributes"),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        _now("updated_at"),
        _fk("entities", "source_id", "sources"),
        sa.UniqueConstraint("source_id", "external_id", name="uq_entities_source_id_external_id"),
    )
    _tenant_index("entities")

    op.create_table(
        "documents",
        *_base("documents"),
        sa.Column("source_id", sa.Uuid(), nullable=False),
        sa.Column("entity_id", sa.Uuid(), nullable=True),
        sa.Column("external_id", sa.String(length=512), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("url", sa.Text(), nullable=True),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        _jsonb("metadata"),
        _now("updated_at"),
        _fk("documents", "source_id", "sources"),
        _fk("documents", "entity_id", "entities"),
        sa.UniqueConstraint("source_id", "external_id", name="uq_documents_source_id_external_id"),
    )
    _tenant_index("documents")
    op.create_index(op.f("ix_documents_entity_id"), "documents", ["entity_id"])

    op.create_table(
        "chunks",
        *_base("chunks"),
        sa.Column("document_id", sa.Uuid(), nullable=False),
        sa.Column("ord", sa.Integer(), nullable=False),
        sa.Column("section", sa.Text(), nullable=True),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("token_count", sa.Integer(), nullable=False),
        _fk("chunks", "document_id", "documents"),
        sa.UniqueConstraint("document_id", "ord", name="uq_chunks_document_id_ord"),
    )
    _tenant_index("chunks")


def downgrade() -> None:
    op.drop_table("chunks")
    op.drop_table("documents")
    op.drop_table("entities")
    op.drop_table("source_syncs")
    op.drop_table("sources")
