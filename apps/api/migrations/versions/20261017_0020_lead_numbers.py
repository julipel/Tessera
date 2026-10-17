"""leads: номер заявки на тенанта, подписи формы, индексы (ADR-0045, REVIEW.md: L-9, L-14)

Revision ID: 0020
Revises: 0019
Create Date: 2026-10-17
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0020"
down_revision: str | None = "0019"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "lead_counters",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("last_number", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            name=op.f("fk_lead_counters_tenant_id_tenants"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_lead_counters")),
        sa.UniqueConstraint("tenant_id", name=op.f("uq_lead_counters_tenant_id")),
    )

    op.add_column("leads", sa.Column("number", sa.Integer(), nullable=True))
    op.add_column("leads", sa.Column("form_title", sa.Text(), nullable=True))
    op.add_column(
        "leads",
        sa.Column(
            "field_labels",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
    )
    # Номера существующих заявок — по порядку создания внутри тенанта.
    op.execute(
        """
        UPDATE leads SET number = numbered.n
        FROM (
            SELECT id, row_number() OVER (PARTITION BY tenant_id ORDER BY created_at, id) AS n
            FROM leads
        ) AS numbered
        WHERE leads.id = numbered.id
        """
    )
    op.execute(
        """
        INSERT INTO lead_counters (id, tenant_id, last_number)
        SELECT gen_random_uuid(), tenant_id, max(number) FROM leads GROUP BY tenant_id
        """
    )
    op.alter_column("leads", "number", nullable=False)

    op.drop_index("ix_leads_tenant_id", table_name="leads")
    op.drop_index("ix_leads_conversation_id", table_name="leads")
    op.drop_index("ix_leads_tenant_id_created_at", table_name="leads")
    op.create_index("ix_leads_tenant_id_created_at_id", "leads", ["tenant_id", "created_at", "id"])
    op.create_index("ix_leads_tenant_id_conversation_id", "leads", ["tenant_id", "conversation_id"])
    op.create_index("uq_leads_tenant_id_number", "leads", ["tenant_id", "number"], unique=True)


def downgrade() -> None:
    op.drop_index("uq_leads_tenant_id_number", table_name="leads")
    op.drop_index("ix_leads_tenant_id_conversation_id", table_name="leads")
    op.drop_index("ix_leads_tenant_id_created_at_id", table_name="leads")
    op.create_index("ix_leads_tenant_id_created_at", "leads", ["tenant_id", "created_at"])
    op.create_index(op.f("ix_leads_conversation_id"), "leads", ["conversation_id"])
    op.create_index(op.f("ix_leads_tenant_id"), "leads", ["tenant_id"])
    op.drop_column("leads", "field_labels")
    op.drop_column("leads", "form_title")
    op.drop_column("leads", "number")
    op.drop_table("lead_counters")
