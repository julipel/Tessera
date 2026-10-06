"""conversations: граница сводки ранней истории (P6-02)

Revision ID: 0010
Revises: 0009
Create Date: 2026-10-06
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0010"
down_revision: str | None = "0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("conversations", sa.Column("summary_message_id", sa.Uuid(), nullable=True))


def downgrade() -> None:
    op.drop_column("conversations", "summary_message_id")
