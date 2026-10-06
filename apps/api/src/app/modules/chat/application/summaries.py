"""Сводка ранней истории диалога (architecture.md §7): что идёт в контекст модели и когда
сворачивать старую часть истории."""

from collections.abc import Sequence
from uuid import UUID

import structlog

from app.contracts import AgentConfig, MemoryConfig
from app.modules.chat.application.conversations import require_conversation
from app.modules.chat.domain.entities import ChatMessage, Conversation, MessageRole
from app.modules.chat.domain.errors import AgentConfigMissingError
from app.modules.chat.domain.ports import (
    AgentConfigSource,
    Commit,
    ConversationStore,
    ConversationSummarizer,
    MessageStore,
)
from app.modules.memory.public import HistoryItem, fold_point
from app.modules.shared.kernel import TenantId

logger = structlog.get_logger(__name__)


def after_summary(
    conversation: Conversation, history: Sequence[ChatMessage]
) -> tuple[str | None, Sequence[ChatMessage]]:
    """Сводка диалога и сообщения после неё. Границы сводки нет в истории — вся история
    без сводки: лучше длинный контекст, чем потерянные сообщения."""
    if conversation.summary_message_id is None or not conversation.summary:
        return None, history
    for i, message in enumerate(history):
        if message.id == conversation.summary_message_id:
            return conversation.summary, history[i + 1 :]
    return None, history


def summary_fold(config: AgentConfig, messages: Sequence[ChatMessage]) -> int | None:
    """Сколько первых сообщений свернуть в сводку по `memory` конфига; None — не нужно."""
    memory = config.memory or MemoryConfig()
    default = MemoryConfig()
    return fold_point(
        [HistoryItem(from_user=m.role is MessageRole.USER, text=m.content) for m in messages],
        threshold_tokens=memory.summary_threshold_tokens or default.summary_threshold_tokens or 0,
        keep_recent_turns=memory.keep_recent_turns or default.keep_recent_turns or 1,
    )


async def summarize_conversation(
    tenant_id: TenantId,
    conversation_id: UUID,
    conversations: ConversationStore,
    messages: MessageStore,
    configs: AgentConfigSource,
    summarizer: ConversationSummarizer,
    commit: Commit,
) -> bool:
    """Свернуть старую часть истории после прошлой сводки, если она длиннее порога.

    True — сводка записана; False — сворачивать нечего, модель не вернула текст или сводку
    уже обновил параллельный запуск. Ошибка провайдера (`LLMError`) не перехватывается."""
    conversation = await require_conversation(tenant_id, conversation_id, conversations)
    config = await configs.config(tenant_id, conversation.agent_config_id)
    if config is None:
        raise AgentConfigMissingError(f"нет AgentConfig {conversation.agent_config_id}")
    previous, pending = after_summary(
        conversation, await messages.list_for(tenant_id, conversation_id)
    )
    point = summary_fold(AgentConfig.model_validate(config), pending)
    if point is None:
        return False
    folded = pending[:point]
    summary = await summarizer.summarize(config, previous, folded)
    if summary is None:
        logger.warning("summary_empty", conversation_id=str(conversation_id))
        return False
    saved = await conversations.update_summary(
        tenant_id, conversation_id, summary, folded[-1].id, conversation.summary_message_id
    )
    await commit()
    logger.info(
        "summary_saved" if saved else "summary_outdated",
        conversation_id=str(conversation_id),
        folded_messages=len(folded),
    )
    return saved
