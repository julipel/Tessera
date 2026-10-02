"""chat: диалоги и сообщения

Revision ID: 0003
Revises: 0002
Create Date: 2026-10-02
"""

from collections.abc import Sequence
from datetime import datetime

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _enum(column: str, *values: str) -> sa.Enum:
    # CHECK задаётся явно в таблице — у типа create_constraint не включаем, иначе дубль.
    return sa.Enum(*values, name=column, native_enum=False, length=16)


def _check(table: str, column: str, *values: str) -> sa.CheckConstraint:
    allowed = ", ".join(f"'{v}'" for v in values)
    return sa.CheckConstraint(f"{column} IN ({allowed})", name=op.f(f"ck_{table}_{column}"))


def _created_at() -> sa.Column[datetime]:
    return sa.Column(
        "created_at",
        sa.DateTime(timezone=True),
        server_default=sa.text("clock_timestamp()"),
        nullable=False,
    )


def _tenant_fk(table: str) -> sa.ForeignKeyConstraint:
    return sa.ForeignKeyConstraint(
        ["tenant_id"],
        ["tenants.id"],
        name=op.f(f"fk_{table}_tenant_id_tenants"),
        ondelete="CASCADE",
    )


def upgrade() -> None:
    op.create_table(
        "conversations",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("agent_config_id", sa.Uuid(), nullable=False),
        sa.Column("channel", _enum("channel", "web"), nullable=False),
        sa.Column("visitor_id", sa.String(length=128), nullable=False),
        sa.Column(
            "state",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("summary", sa.Text(), nullable=True),
        _created_at(),
        _check("conversations", "channel", "web"),
        _tenant_fk("conversations"),
        sa.ForeignKeyConstraint(
            ["agent_config_id"],
            ["agent_configs.id"],
            name=op.f("fk_conversations_agent_config_id_agent_configs"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_conversations")),
    )
    op.create_index(op.f("ix_conversations_tenant_id"), "conversations", ["tenant_id"])
    op.create_index(
        "ix_conversations_tenant_id_visitor_id", "conversations", ["tenant_id", "visitor_id"]
    )
    op.create_table(
        "messages",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("conversation_id", sa.Uuid(), nullable=False),
        sa.Column("role", _enum("role", "user", "assistant"), nullable=False),
        sa.Column("status", _enum("status", "completed", "interrupted", "failed"), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("input", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column(
            "blocks",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column("client_message_id", sa.Uuid(), nullable=True),
        _created_at(),
        _check("messages", "role", "user", "assistant"),
        _check("messages", "status", "completed", "interrupted", "failed"),
        _tenant_fk("messages"),
        sa.ForeignKeyConstraint(
            ["conversation_id"],
            ["conversations.id"],
            name=op.f("fk_messages_conversation_id_conversations"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_messages")),
        sa.UniqueConstraint(
            "conversation_id",
            "client_message_id",
            name="uq_messages_conversation_id_client_message_id",
        ),
    )
    op.create_index(op.f("ix_messages_tenant_id"), "messages", ["tenant_id"])
    op.create_index(
        "ix_messages_conversation_id_created_at", "messages", ["conversation_id", "created_at"]
    )


def downgrade() -> None:
    op.drop_table("messages")
    op.drop_table("conversations")
