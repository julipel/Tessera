"""Use cases диалога: начало, история, идемпотентная запись ввода пользователя."""

from uuid import UUID

from app.contracts import HistoryMessage, MessageHistory, TextInput, UserInput
from app.modules.chat.domain.entities import (
    Channel,
    ChatMessage,
    Conversation,
    MessageRole,
    MessageStatus,
    NewMessage,
)
from app.modules.chat.domain.errors import ConversationNotFoundError, NoActiveConfigError
from app.modules.chat.domain.ports import ActiveConfigLookup, ConversationStore, MessageStore
from app.modules.shared.kernel import TenantId


async def start_conversation(
    tenant_id: TenantId,
    visitor_id: str,
    conversations: ConversationStore,
    configs: ActiveConfigLookup,
) -> Conversation:
    config_id = await configs.active_config_id(tenant_id)
    if config_id is None:
        raise NoActiveConfigError(f"у тенанта {tenant_id} нет активного AgentConfig")
    return await conversations.create(tenant_id, config_id, Channel.WEB, visitor_id)


async def require_conversation(
    tenant_id: TenantId, conversation_id: UUID, conversations: ConversationStore
) -> Conversation:
    conversation = await conversations.find(tenant_id, conversation_id)
    if conversation is None:
        raise ConversationNotFoundError(f"диалог {conversation_id} не найден")
    return conversation


async def get_history(
    tenant_id: TenantId,
    conversation_id: UUID,
    conversations: ConversationStore,
    messages: MessageStore,
) -> MessageHistory:
    await require_conversation(tenant_id, conversation_id, conversations)
    items = await messages.list_for(tenant_id, conversation_id)
    return MessageHistory(
        conversation_id=conversation_id, messages=[_history_message(m) for m in items]
    )


def _history_message(message: ChatMessage) -> HistoryMessage:
    return HistoryMessage.model_validate(
        {
            "message_id": message.id,
            "role": message.role,
            "status": message.status,
            "created_at": message.created_at,
            "input": message.input,
            "blocks": message.blocks,
            "error": message.error,
        }
    )


async def append_user_message(
    tenant_id: TenantId,
    conversation_id: UUID,
    client_message_id: UUID,
    user_input: UserInput,
    conversations: ConversationStore,
    messages: MessageStore,
) -> tuple[ChatMessage, bool]:
    """Повтор с тем же client_message_id (ретрай клиента) возвращает уже записанное
    сообщение, даже если ввод отличается: идемпотентность по ключу, а не по содержимому."""
    await require_conversation(tenant_id, conversation_id, conversations)
    return await messages.add_once(
        tenant_id, user_message(conversation_id, client_message_id, user_input)
    )


def user_message(
    conversation_id: UUID, client_message_id: UUID, user_input: UserInput
) -> NewMessage:
    payload = user_input.root
    return NewMessage(
        conversation_id=conversation_id,
        role=MessageRole.USER,
        status=MessageStatus.COMPLETED,
        content=payload.text if isinstance(payload, TextInput) else "",
        input=user_input.model_dump(mode="json", exclude_none=True),
        client_message_id=client_message_id,
    )
