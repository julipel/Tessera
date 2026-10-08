"""admin_users, admin_memberships: пользователи админки и роли в тенантах (P8-01a, ADR-0036)

Revision ID: 0013
Revises: 0012
Create Date: 2026-10-08
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0013"
down_revision: str | None = "0012"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "admin_users",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("email", sa.String(length=320), nullable=False),
        sa.Column("password_hash", sa.String(length=255), nullable=False),
        sa.Column("is_superadmin", sa.Boolean(), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_admin_users")),
        sa.UniqueConstraint("email", name=op.f("uq_admin_users_email")),
    )
    op.create_table(
        "admin_memberships",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        # VARCHAR + CHECK вместо нативного ENUM — как статусы в tenants.
        sa.Column(
            "role",
            sa.Enum("viewer", "editor", name="role", native_enum=False, length=16),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("role IN ('viewer', 'editor')", name=op.f("ck_admin_memberships_role")),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            name=op.f("fk_admin_memberships_tenant_id_tenants"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["admin_users.id"],
            name=op.f("fk_admin_memberships_user_id_admin_users"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_admin_memberships")),
        sa.UniqueConstraint("tenant_id", "user_id", name="uq_admin_memberships_tenant_id_user_id"),
    )
    op.create_index(op.f("ix_admin_memberships_tenant_id"), "admin_memberships", ["tenant_id"])
    op.create_index(op.f("ix_admin_memberships_user_id"), "admin_memberships", ["user_id"])


def downgrade() -> None:
    op.drop_table("admin_memberships")
    op.drop_table("admin_users")
