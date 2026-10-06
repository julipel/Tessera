"""ORM-модели модуля chat."""

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import (
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.modules.chat.domain.entities import Channel, MessageRole, MessageStatus
from app.modules.shared.public import TenantScopedBase


def _str_enum[E: (Channel, MessageRole, MessageStatus)](enum: type[E], name: str) -> Enum:
    # VARCHAR + CHECK вместо нативного ENUM Postgres: новые значения без ALTER TYPE.
    return Enum(
        enum,
        name=name,
        native_enum=False,
        create_constraint=True,
        length=16,
        values_callable=lambda e: [m.value for m in e],
    )


# clock_timestamp(), а не now(): now() — время начала транзакции, и у сообщений одного хода
# оно совпало бы, а порядок истории стал бы неопределённым.
_CREATED_AT = text("clock_timestamp()")


class ConversationRecord(TenantScopedBase):
    __tablename__ = "conversations"
    __table_args__ = (Index("ix_conversations_tenant_id_visitor_id", "tenant_id", "visitor_id"),)

    tenant_id: Mapped[UUID] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), index=True
    )
    agent_config_id: Mapped[UUID] = mapped_column(
        ForeignKey("agent_configs.id", ondelete="RESTRICT")
    )
    channel: Mapped[Channel] = mapped_column(_str_enum(Channel, "channel"))
    visitor_id: Mapped[str] = mapped_column(String(128))
    state: Mapped[dict[str, Any]] = mapped_column(
        JSONB, default=dict, server_default=text("'{}'::jsonb")
    )
    summary: Mapped[str | None] = mapped_column(Text)
    # Последнее сообщение, свёрнутое в summary (P6-02); без FK — messages ссылаются на диалог.
    summary_message_id: Mapped[UUID | None] = mapped_column()
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=_CREATED_AT
    )


class MessageRecord(TenantScopedBase):
    __tablename__ = "messages"
    __table_args__ = (
        # Идемпотентность записи ввода; NULL (сообщения ассистента) не конфликтуют.
        UniqueConstraint(
            "conversation_id",
            "client_message_id",
            name="uq_messages_conversation_id_client_message_id",
        ),
        Index("ix_messages_conversation_id_created_at", "conversation_id", "created_at"),
    )

    tenant_id: Mapped[UUID] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), index=True
    )
    conversation_id: Mapped[UUID] = mapped_column(
        ForeignKey("conversations.id", ondelete="CASCADE")
    )
    role: Mapped[MessageRole] = mapped_column(_str_enum(MessageRole, "role"))
    status: Mapped[MessageStatus] = mapped_column(_str_enum(MessageStatus, "status"))
    content: Mapped[str] = mapped_column(Text, default="")
    input: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    blocks: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, default=list, server_default=text("'[]'::jsonb")
    )
    client_message_id: Mapped[UUID | None]
    # Ошибка хода у неудачного ответа: {code, message, retryable}.
    error: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    # id ответа, заменившего этот при повторе (ADR-0023). Без FK: новый ответ записывается
    # в конце хода, а пометка ставится до его начала.
    replaced_by: Mapped[UUID | None]
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=_CREATED_AT
    )


class ToolCallRecord(TenantScopedBase):
    __tablename__ = "tool_calls"
    __table_args__ = (Index("ix_tool_calls_message_id_created_at", "message_id", "created_at"),)

    tenant_id: Mapped[UUID] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), index=True
    )
    message_id: Mapped[UUID] = mapped_column(ForeignKey("messages.id", ondelete="CASCADE"))
    tool_call_id: Mapped[str] = mapped_column(String(128))
    name: Mapped[str] = mapped_column(String(128))
    # JSONB хранит и объект, и строку: неразобранные аргументы, текстовый результат.
    arguments: Mapped[dict[str, Any] | str] = mapped_column(JSONB)
    result: Mapped[dict[str, Any] | str | None] = mapped_column(JSONB)
    error: Mapped[dict[str, str] | None] = mapped_column(JSONB)
    duration_ms: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=_CREATED_AT
    )
