"""knowledge: не больше одной активной синхронизации на источник

Revision ID: 0006
Revises: 0005
Create Date: 2026-10-04
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_index(
        "uq_source_syncs_source_id_active",
        "source_syncs",
        ["source_id"],
        unique=True,
        postgresql_where=sa.text("status IN ('pending', 'running')"),
    )


def downgrade() -> None:
    op.drop_index("uq_source_syncs_source_id_active", table_name="source_syncs")
