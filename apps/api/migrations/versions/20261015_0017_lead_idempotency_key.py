"""leads: ключ идемпотентности заявки — confirm_id подтверждения (P9-01g)

Revision ID: 0017
Revises: 0016
Create Date: 2026-10-15
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0017"
down_revision: str | None = "0016"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("leads", sa.Column("idempotency_key", sa.String(length=64), nullable=True))
    op.create_index(
        "uq_leads_tenant_conversation_idempotency_key",
        "leads",
        ["tenant_id", "conversation_id", "idempotency_key"],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index("uq_leads_tenant_conversation_idempotency_key", table_name="leads")
    op.drop_column("leads", "idempotency_key")
