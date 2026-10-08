"""knowledge: откуда источник (YAML тенанта или админка) и время запроса синхронизации
(P8-03a, ADR-0037)

Revision ID: 0015
Revises: 0014
Create Date: 2026-10-08
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0015"
down_revision: str | None = "0014"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Источники до P8-03 создавал только seed.
    op.add_column(
        "sources",
        sa.Column(
            "origin",
            sa.Enum("yaml", "admin", name="origin", native_enum=False, length=16),
            server_default="yaml",
            nullable=False,
        ),
    )
    op.create_check_constraint(op.f("ck_sources_origin"), "sources", "origin IN ('yaml', 'admin')")
    op.add_column(
        "source_syncs",
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("clock_timestamp()"),
            nullable=False,
        ),
    )
    op.execute("UPDATE source_syncs SET created_at = started_at WHERE started_at IS NOT NULL")
    op.create_index(
        "ix_source_syncs_source_id_created_at", "source_syncs", ["source_id", "created_at"]
    )


def downgrade() -> None:
    op.drop_index("ix_source_syncs_source_id_created_at", table_name="source_syncs")
    op.drop_column("source_syncs", "created_at")
    op.drop_constraint(op.f("ck_sources_origin"), "sources", type_="check")
    op.drop_column("sources", "origin")
