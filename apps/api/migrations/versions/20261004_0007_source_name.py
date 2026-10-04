"""knowledge: имя источника — ключ декларации в YAML тенанта (ADR-0019)

Revision ID: 0007
Revises: 0006
Create Date: 2026-10-04
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("sources", sa.Column("name", sa.String(length=64), nullable=True))
    op.create_index(
        "uq_sources_tenant_id_name",
        "sources",
        ["tenant_id", "name"],
        unique=True,
        postgresql_where=sa.text("name IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("uq_sources_tenant_id_name", table_name="sources")
    op.drop_column("sources", "name")
