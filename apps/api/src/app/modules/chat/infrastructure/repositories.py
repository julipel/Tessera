"""Репозитории модуля chat (реализации портов из domain/ports.py)."""

from collections.abc import Sequence
from typing import Any
from uuid import UUID

from sqlalchemy import update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.chat.domain.entities import (
    Channel,
    ChatMessage,
    Conversation,
    NewMessage,
    ToolCallEntry,
)
from app.modules.chat.infrastructure.models import (
    ConversationRecord,
    MessageRecord,
    ToolCallRecord,
)
from app.modules.shared.public import TenantId, TenantRepository
from app.modules.tenants.public import AgentConfigRepository


def _conversation(record: ConversationRecord) -> Conversation:
    return Conversation(
        id=record.id,
        tenant_id=TenantId(record.tenant_id),
        agent_config_id=record.agent_config_id,
        channel=record.channel,
        visitor_id=record.visitor_id,
        created_at=record.created_at,
        state=record.state,
        summary=record.summary,
        summary_message_id=record.summary_message_id,
    )


def _message(record: MessageRecord) -> ChatMessage:
    return ChatMessage(
        id=record.id,
        tenant_id=TenantId(record.tenant_id),
        conversation_id=record.conversation_id,
        role=record.role,
        status=record.status,
        content=record.content,
        input=record.input,
        blocks=record.blocks,
        client_message_id=record.client_message_id,
        created_at=record.created_at,
        error=record.error,
    )


class ConversationRepository(TenantRepository[ConversationRecord]):
    model = ConversationRecord

    async def create(
        self, tenant_id: TenantId, agent_config_id: UUID, channel: Channel, visitor_id: str
    ) -> Conversation:
        record = ConversationRecord(
            tenant_id=tenant_id,
            agent_config_id=agent_config_id,
            channel=channel,
            visitor_id=visitor_id,
        )
        await self.add(tenant_id, record)
        await self.session.refresh(record)  # created_at задаёт БД
        return _conversation(record)

    async def find(self, tenant_id: TenantId, conversation_id: UUID) -> Conversation | None:
        record = await self.get(tenant_id, conversation_id)
        return _conversation(record) if record else None

    async def update_state(
        self, tenant_id: TenantId, conversation_id: UUID, state: dict[str, Any]
    ) -> None:
        stmt = (
            update(ConversationRecord)
            .where(
                ConversationRecord.tenant_id == tenant_id,
                ConversationRecord.id == conversation_id,
            )
            .values(state=state)
        )
        await self.session.execute(stmt)

    async def update_summary(
        self,
        tenant_id: TenantId,
        conversation_id: UUID,
        summary: str,
        through_message_id: UUID,
        expected_through: UUID | None,
    ) -> bool:
        # Условный UPDATE, как mark_replaced: опоздавшая сводка не затрёт более свежую.
        expected = (
            ConversationRecord.summary_message_id.is_(None)
            if expected_through is None
            else ConversationRecord.summary_message_id == expected_through
        )
        stmt = (
            update(ConversationRecord)
            .where(
                ConversationRecord.tenant_id == tenant_id,
                ConversationRecord.id == conversation_id,
                expected,
            )
            .values(summary=summary, summary_message_id=through_message_id)
            .returning(ConversationRecord.id)
        )
        return (await self.session.execute(stmt)).scalar_one_or_none() is not None


class MessageRepository(TenantRepository[MessageRecord]):
    model = MessageRecord

    async def list_for(self, tenant_id: TenantId, conversation_id: UUID) -> Sequence[ChatMessage]:
        stmt = (
            self._scoped(tenant_id)
            .where(
                MessageRecord.conversation_id == conversation_id,
                MessageRecord.replaced_by.is_(None),
            )
            .order_by(MessageRecord.created_at, MessageRecord.id)
        )
        return [_message(r) for r in (await self.session.execute(stmt)).scalars()]

    async def mark_replaced(self, tenant_id: TenantId, message_id: UUID, replaced_by: UUID) -> bool:
        # Условный UPDATE вместо «прочитать, затем записать»: из двух одновременных повторов
        # строку обновит только первый.
        stmt = (
            update(MessageRecord)
            .where(
                MessageRecord.tenant_id == tenant_id,
                MessageRecord.id == message_id,
                MessageRecord.replaced_by.is_(None),
            )
            .values(replaced_by=replaced_by)
            .returning(MessageRecord.id)
        )
        return (await self.session.execute(stmt)).scalar_one_or_none() is not None

    async def add_once(self, tenant_id: TenantId, message: NewMessage) -> tuple[ChatMessage, bool]:
        # ON CONFLICT DO NOTHING вместо «проверить, затем вставить»: два одновременных
        # ретрая не упадут на уникальном индексе, второй получит запись первого.
        stmt = (
            insert(MessageRecord)
            .values(
                # id задан, когда он заранее отдан клиенту (message_id в SSE-событиях хода).
                **({"id": message.id} if message.id is not None else {}),
                tenant_id=tenant_id,
                conversation_id=message.conversation_id,
                role=message.role,
                status=message.status,
                content=message.content,
                input=message.input,
                blocks=list(message.blocks),
                client_message_id=message.client_message_id,
                error=message.error,
            )
            .on_conflict_do_nothing(index_elements=["conversation_id", "client_message_id"])
            .returning(MessageRecord)
        )
        created = (await self.session.execute(stmt)).scalar_one_or_none()
        if created is not None:
            return _message(created), True
        existing = await self.session.execute(
            self._scoped(tenant_id).where(
                MessageRecord.conversation_id == message.conversation_id,
                MessageRecord.client_message_id == message.client_message_id,
            )
        )
        return _message(existing.scalar_one()), False


class ToolCallRepository(TenantRepository[ToolCallRecord]):
    model = ToolCallRecord

    async def add_many(
        self, tenant_id: TenantId, message_id: UUID, calls: Sequence[ToolCallEntry]
    ) -> None:
        for call in calls:
            await self.add(
                tenant_id,
                ToolCallRecord(
                    tenant_id=tenant_id,
                    message_id=message_id,
                    tool_call_id=call.tool_call_id,
                    name=call.name,
                    arguments=call.arguments,
                    result=call.result,
                    error=call.error,
                    duration_ms=call.duration_ms,
                ),
            )

    async def list_for(self, tenant_id: TenantId, message_id: UUID) -> Sequence[ToolCallEntry]:
        stmt = (
            self._scoped(tenant_id)
            .where(ToolCallRecord.message_id == message_id)
            .order_by(ToolCallRecord.created_at, ToolCallRecord.id)
        )
        return [
            ToolCallEntry(
                tool_call_id=r.tool_call_id,
                name=r.name,
                arguments=r.arguments,
                result=r.result,
                error=r.error,
                duration_ms=r.duration_ms,
            )
            for r in (await self.session.execute(stmt)).scalars()
        ]


class TenantsActiveConfig:
    """ActiveConfigLookup поверх модуля tenants."""

    def __init__(self, session: AsyncSession) -> None:
        self.configs = AgentConfigRepository(session)

    async def active_config_id(self, tenant_id: TenantId) -> UUID | None:
        active = await self.configs.get_active(tenant_id)
        return active.id if active else None


class TenantsAgentConfigs:
    """AgentConfigSource поверх модуля tenants."""

    def __init__(self, session: AsyncSession) -> None:
        self.configs = AgentConfigRepository(session)

    async def config(self, tenant_id: TenantId, config_id: UUID) -> dict[str, Any] | None:
        version = await self.configs.get_version(tenant_id, config_id)
        return version.config if version else None
