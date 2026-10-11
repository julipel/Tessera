"""chat: оценки ответов ассистента сотрудниками админки (P8-04a, ADR-0038)

Revision ID: 0016
Revises: 0015
Create Date: 2026-10-11
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0016"
down_revision: str | None = "0015"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "message_ratings",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("conversation_id", sa.Uuid(), nullable=False),
        sa.Column("message_id", sa.Uuid(), nullable=False),
        sa.Column(
            "rating",
            sa.Enum("good", "bad", name="rating", native_enum=False, length=16),
            nullable=False,
        ),
        sa.Column("comment", sa.Text(), nullable=True),
        sa.Column("rated_by", sa.Uuid(), nullable=True),
        sa.Column("rated_by_email", sa.String(length=320), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("clock_timestamp()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("clock_timestamp()"),
            nullable=False,
        ),
        sa.CheckConstraint("rating IN ('good', 'bad')", name=op.f("ck_message_ratings_rating")),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            name=op.f("fk_message_ratings_tenant_id_tenants"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["conversation_id"],
            ["conversations.id"],
            name=op.f("fk_message_ratings_conversation_id_conversations"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["message_id"],
            ["messages.id"],
            name=op.f("fk_message_ratings_message_id_messages"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["rated_by"],
            ["admin_users.id"],
            name=op.f("fk_message_ratings_rated_by_admin_users"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_message_ratings")),
        sa.UniqueConstraint("message_id", name="uq_message_ratings_message_id"),
    )
    op.create_index(op.f("ix_message_ratings_tenant_id"), "message_ratings", ["tenant_id"])
    op.create_index(
        "ix_message_ratings_tenant_id_conversation_id",
        "message_ratings",
        ["tenant_id", "conversation_id"],
    )
    # Список диалогов админки — от новых к старым по (created_at, id) в тенанте.
    op.create_index(
        "ix_conversations_tenant_id_created_at_id",
        "conversations",
        ["tenant_id", "created_at", "id"],
    )


def downgrade() -> None:
    op.drop_index("ix_conversations_tenant_id_created_at_id", table_name="conversations")
    op.drop_table("message_ratings")
