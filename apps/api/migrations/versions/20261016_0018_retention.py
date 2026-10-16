"""retention (ADR-0043): заявки переживают удаление диалога, индексы под удаление

Revision ID: 0018
Revises: 0017
Create Date: 2026-10-16
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0018"
down_revision: str | None = "0017"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # L-4: удаление диалога по сроку не уносит заявку — у неё свой срок хранения.
    op.alter_column("leads", "conversation_id", existing_type=sa.Uuid(), nullable=True)
    op.drop_constraint("fk_leads_conversation_id_conversations", "leads", type_="foreignkey")
    op.create_foreign_key(
        "fk_leads_conversation_id_conversations",
        "leads",
        "conversations",
        ["conversation_id"],
        ["id"],
        ondelete="SET NULL",
    )
    # O-9: префикс tenant_id уже есть в составных индексах; удаление по сроку идёт по ts,
    # каскад от диалога — по conversation_id без tenant_id.
    op.drop_index("ix_agent_events_tenant_id", table_name="agent_events")
    op.create_index("ix_agent_events_tenant_id_ts", "agent_events", ["tenant_id", "ts"])
    op.create_index("ix_agent_events_conversation_id", "agent_events", ["conversation_id"])
    # Тот же каскад от диалога у оценок: есть только (tenant_id, conversation_id).
    op.create_index("ix_message_ratings_conversation_id", "message_ratings", ["conversation_id"])


def downgrade() -> None:
    op.drop_index("ix_message_ratings_conversation_id", table_name="message_ratings")
    op.drop_index("ix_agent_events_conversation_id", table_name="agent_events")
    op.drop_index("ix_agent_events_tenant_id_ts", table_name="agent_events")
    op.create_index("ix_agent_events_tenant_id", "agent_events", ["tenant_id"])
    op.drop_constraint("fk_leads_conversation_id_conversations", "leads", type_="foreignkey")
    # Заявки без диалога (диалог удалён по сроку) не вписываются в NOT NULL.
    op.execute("DELETE FROM leads WHERE conversation_id IS NULL")
    op.create_foreign_key(
        "fk_leads_conversation_id_conversations",
        "leads",
        "conversations",
        ["conversation_id"],
        ["id"],
        ondelete="CASCADE",
    )
    op.alter_column("leads", "conversation_id", existing_type=sa.Uuid(), nullable=False)
