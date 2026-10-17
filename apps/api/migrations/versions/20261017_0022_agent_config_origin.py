"""agent_configs.origin: версия из seed или админки — seed не затирает правки (REVIEW.md: R-1)

Revision ID: 0022
Revises: 0021
Create Date: 2026-10-17
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0022"
down_revision: str | None = "0021"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Происхождение существующих версий неизвестно — считаем их правками админки: seed
    # не перезапишет их без --force-config (лучше не применить YAML, чем потерять правку).
    op.add_column(
        "agent_configs",
        sa.Column(
            "origin",
            sa.Enum("seed", "admin", name="origin", native_enum=False, length=16),
            server_default="admin",
            nullable=False,
        ),
    )
    op.alter_column("agent_configs", "origin", server_default=None)
    op.create_check_constraint(
        op.f("ck_agent_configs_origin"), "agent_configs", "origin IN ('seed', 'admin')"
    )


def downgrade() -> None:
    op.drop_constraint(op.f("ck_agent_configs_origin"), "agent_configs", type_="check")
    op.drop_column("agent_configs", "origin")
