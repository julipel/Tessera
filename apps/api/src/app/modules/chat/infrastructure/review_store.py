"""ConversationReviewStore на SQLAlchemy (ADR-0038): каждый запрос ограничен тенантом."""

from collections import defaultdict
from typing import Any
from uuid import UUID

from sqlalchemy import Select, and_, delete, exists, func, select, text, tuple_
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.chat.domain.entities import ChatMessage, MessageRole, ToolCallEntry
from app.modules.chat.domain.review import (
    PREVIEW_CHARS,
    ConversationOverview,
    ConversationReview,
    MessageRatingEntry,
    PageCursor,
    Rating,
    ReviewedMessage,
)
from app.modules.chat.infrastructure.models import (
    ConversationRecord,
    MessageRatingRecord,
    MessageRecord,
    ToolCallRecord,
)
from app.modules.chat.infrastructure.repositories import conversation_entity, message_entity
from app.modules.shared.public import TenantId
from app.modules.tenants.public import AgentConfigRecord


def _rating_entry(record: MessageRatingRecord) -> MessageRatingEntry:
    return MessageRatingEntry(
        message_id=record.message_id,
        rating=record.rating,
        comment=record.comment,
        rated_by=record.rated_by_email,
        updated_at=record.updated_at,
    )


class SqlConversationReviewStore:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    def _overviews(self, tenant_id: TenantId) -> Select[Any]:
        c = ConversationRecord
        m = MessageRecord
        r = MessageRatingRecord
        in_conversation = and_(m.tenant_id == tenant_id, m.conversation_id == c.id)

        def ratings(value: Rating) -> Any:
            return (
                select(func.count())
                .where(r.tenant_id == tenant_id, r.conversation_id == c.id, r.rating == value)
                .scalar_subquery()
            )

        return (
            select(
                c,
                AgentConfigRecord.version,
                select(func.count()).where(in_conversation).scalar_subquery(),
                select(func.max(m.created_at)).where(in_conversation).scalar_subquery(),
                select(func.left(m.content, PREVIEW_CHARS))
                .where(in_conversation, m.role == MessageRole.USER)
                .order_by(m.created_at, m.id)
                .limit(1)
                .scalar_subquery(),
                ratings(Rating.GOOD),
                ratings(Rating.BAD),
            )
            .join(
                AgentConfigRecord,
                and_(
                    AgentConfigRecord.id == c.agent_config_id,
                    AgentConfigRecord.tenant_id == tenant_id,
                ),
            )
            .where(c.tenant_id == tenant_id)
        )

    @staticmethod
    def _overview(row: Any) -> ConversationOverview:
        record, version, messages, last_at, preview, good, bad = row
        return ConversationOverview(
            conversation=conversation_entity(record),
            agent_config_version=version,
            messages=messages,
            last_message_at=last_at,
            preview=preview or None,
            good=good,
            bad=bad,
        )

    async def list_conversations(
        self,
        tenant_id: TenantId,
        *,
        before: PageCursor | None,
        limit: int,
        rating: Rating | None,
    ) -> list[ConversationOverview]:
        c = ConversationRecord
        stmt = self._overviews(tenant_id)
        if before is not None:
            stmt = stmt.where(tuple_(c.created_at, c.id) < tuple_(before.created_at, before.id))
        if rating is not None:
            r = MessageRatingRecord
            stmt = stmt.where(
                exists().where(
                    r.tenant_id == tenant_id, r.conversation_id == c.id, r.rating == rating
                )
            )
        stmt = stmt.order_by(c.created_at.desc(), c.id.desc()).limit(limit)
        return [self._overview(row) for row in await self.session.execute(stmt)]

    async def review(self, tenant_id: TenantId, conversation_id: UUID) -> ConversationReview | None:
        stmt = self._overviews(tenant_id).where(ConversationRecord.id == conversation_id)
        row = (await self.session.execute(stmt)).one_or_none()
        if row is None:
            return None
        messages = [
            message_entity(r)
            for r in await self.session.scalars(
                select(MessageRecord)
                .where(
                    MessageRecord.tenant_id == tenant_id,
                    MessageRecord.conversation_id == conversation_id,
                )
                .order_by(MessageRecord.created_at, MessageRecord.id)
            )
        ]
        ids = [m.id for m in messages]
        calls: defaultdict[UUID, list[ToolCallEntry]] = defaultdict(list)
        for call in await self.session.scalars(
            select(ToolCallRecord)
            .where(ToolCallRecord.tenant_id == tenant_id, ToolCallRecord.message_id.in_(ids))
            .order_by(ToolCallRecord.created_at, ToolCallRecord.id)
        ):
            calls[call.message_id].append(
                ToolCallEntry(
                    tool_call_id=call.tool_call_id,
                    name=call.name,
                    arguments=call.arguments,
                    result=call.result,
                    error=call.error,
                    duration_ms=call.duration_ms,
                )
            )
        ratings = {
            r.message_id: _rating_entry(r)
            for r in await self.session.scalars(
                select(MessageRatingRecord).where(
                    MessageRatingRecord.tenant_id == tenant_id,
                    MessageRatingRecord.conversation_id == conversation_id,
                )
            )
        }
        return ConversationReview(
            overview=self._overview(row),
            messages=[ReviewedMessage(m, calls[m.id], ratings.get(m.id)) for m in messages],
        )

    async def find_message(
        self, tenant_id: TenantId, conversation_id: UUID, message_id: UUID
    ) -> ChatMessage | None:
        record = await self.session.scalar(
            select(MessageRecord).where(
                MessageRecord.tenant_id == tenant_id,
                MessageRecord.conversation_id == conversation_id,
                MessageRecord.id == message_id,
            )
        )
        return message_entity(record) if record else None

    async def put_rating(
        self,
        tenant_id: TenantId,
        conversation_id: UUID,
        message_id: UUID,
        rating: Rating,
        comment: str | None,
        rated_by: UUID,
        rated_by_email: str,
    ) -> MessageRatingEntry:
        # Upsert, а не «прочитать, затем записать»: две одновременные оценки не упадут
        # на уникальном индексе, останется последняя.
        values = {
            "rating": rating,
            "comment": comment,
            "rated_by": rated_by,
            "rated_by_email": rated_by_email,
        }
        stmt = (
            insert(MessageRatingRecord)
            .values(
                tenant_id=tenant_id,
                conversation_id=conversation_id,
                message_id=message_id,
                **values,
            )
            .on_conflict_do_update(
                index_elements=["message_id"],
                set_={**values, "updated_at": text("clock_timestamp()")},
                where=MessageRatingRecord.tenant_id == tenant_id,
            )
            .returning(MessageRatingRecord)
            .execution_options(populate_existing=True)
        )
        record = (await self.session.execute(stmt)).scalar_one()
        return _rating_entry(record)

    async def delete_rating(self, tenant_id: TenantId, message_id: UUID) -> bool:
        stmt = (
            delete(MessageRatingRecord)
            .where(
                MessageRatingRecord.tenant_id == tenant_id,
                MessageRatingRecord.message_id == message_id,
            )
            .returning(MessageRatingRecord.id)
        )
        return (await self.session.execute(stmt)).scalar_one_or_none() is not None
