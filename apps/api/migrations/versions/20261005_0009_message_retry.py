"""messages: ошибка хода и пометка замены повтором (ADR-0023)

Revision ID: 0009
Revises: 0008
Create Date: 2026-10-05
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0009"
down_revision: str | None = "0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "messages", sa.Column("error", postgresql.JSONB(astext_type=sa.Text()), nullable=True)
    )
    op.add_column("messages", sa.Column("replaced_by", sa.Uuid(), nullable=True))


def downgrade() -> None:
    op.drop_column("messages", "replaced_by")
    op.drop_column("messages", "error")
