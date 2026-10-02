"""tenants: тенанты, версии AgentConfig, ключи виджета

Revision ID: 0002
Revises: 0001
Create Date: 2026-10-02
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _status(*values: str) -> sa.Enum:
    # CHECK задаётся явно в таблице — у типа create_constraint не включаем, иначе дубль.
    return sa.Enum(*values, name="status", native_enum=False, length=16)


def _check_status(table: str, *values: str) -> sa.CheckConstraint:
    allowed = ", ".join(f"'{v}'" for v in values)
    return sa.CheckConstraint(f"status IN ({allowed})", name=op.f(f"ck_{table}_status"))


def upgrade() -> None:
    op.create_table(
        "tenants",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("slug", sa.String(length=64), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("status", _status("active", "disabled"), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        _check_status("tenants", "active", "disabled"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_tenants")),
        sa.UniqueConstraint("slug", name=op.f("uq_tenants_slug")),
    )
    op.create_table(
        "agent_configs",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("status", _status("draft", "active", "archived"), nullable=False),
        sa.Column("config", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        _check_status("agent_configs", "draft", "active", "archived"),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            name=op.f("fk_agent_configs_tenant_id_tenants"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_agent_configs")),
        sa.UniqueConstraint("tenant_id", "version", name="uq_agent_configs_tenant_id_version"),
    )
    op.create_index(op.f("ix_agent_configs_tenant_id"), "agent_configs", ["tenant_id"])
    op.create_index(
        "uq_agent_configs_one_active",
        "agent_configs",
        ["tenant_id"],
        unique=True,
        postgresql_where=sa.text("status = 'active'"),
    )
    op.create_table(
        "widget_keys",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("key_hash", sa.String(length=64), nullable=False),
        sa.Column("allowed_origins", postgresql.ARRAY(sa.String()), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            name=op.f("fk_widget_keys_tenant_id_tenants"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_widget_keys")),
        sa.UniqueConstraint("key_hash", name=op.f("uq_widget_keys_key_hash")),
    )
    op.create_index(op.f("ix_widget_keys_tenant_id"), "widget_keys", ["tenant_id"])


def downgrade() -> None:
    op.drop_table("widget_keys")
    op.drop_table("agent_configs")
    op.drop_table("tenants")
