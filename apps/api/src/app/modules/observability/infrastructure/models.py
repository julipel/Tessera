"""ORM-модели модуля observability."""

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import DateTime, ForeignKey, Index, Integer, String
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.modules.shared.public import TenantScopedBase


class AgentEventRecord(TenantScopedBase):
    __tablename__ = "agent_events"
    __table_args__ = (
        Index("ix_agent_events_tenant_id_conversation_id_ts", "tenant_id", "conversation_id", "ts"),
        Index("ix_agent_events_tenant_id_turn_id", "tenant_id", "turn_id"),
        Index("ix_agent_events_tenant_id_trace_id", "tenant_id", "trace_id"),
    )

    tenant_id: Mapped[UUID] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), index=True
    )
    conversation_id: Mapped[UUID] = mapped_column(
        ForeignKey("conversations.id", ondelete="CASCADE")
    )
    turn_id: Mapped[UUID]
    trace_id: Mapped[str] = mapped_column(String(128))
    seq: Mapped[int] = mapped_column(Integer)
    type: Mapped[str] = mapped_column(String(32))
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True))
