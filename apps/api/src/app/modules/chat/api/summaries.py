"""Сборка запуска сводки для SummaryScheduler: своя сессия БД на сводку (ADR-0024)."""

from collections.abc import Callable
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.chat.application.summaries import SummaryRun, summarize_conversation
from app.modules.chat.infrastructure.loop_agent import LLMForProvider
from app.modules.chat.infrastructure.repositories import (
    ConversationRepository,
    MessageRepository,
    TenantsAgentConfigs,
)
from app.modules.chat.infrastructure.summarizer import LoopConversationSummarizer
from app.modules.shared.kernel import TenantId


def background_summary(
    session_factory: Callable[[], AsyncSession], llm_for: LLMForProvider
) -> SummaryRun:
    """Сессия стрима хода к моменту сводки уже закрыта или занята — сводка открывает свою."""
    summarizer = LoopConversationSummarizer(llm_for)

    async def run(tenant_id: TenantId, conversation_id: UUID) -> bool:
        async with session_factory() as session:
            return await summarize_conversation(
                tenant_id,
                conversation_id,
                ConversationRepository(session),
                MessageRepository(session),
                TenantsAgentConfigs(session),
                summarizer,
                session.commit,
            )

    return run
