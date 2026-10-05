"""Репозитории chat: изоляция тенантов (ADR-0006), порядок истории, идемпотентная запись ввода."""

from uuid import UUID, uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.contracts import UserInput
from app.modules.chat.application.conversations import append_user_message
from app.modules.chat.domain.entities import (
    Channel,
    Conversation,
    MessageRole,
    MessageStatus,
    NewMessage,
    ToolCallEntry,
)
from app.modules.chat.domain.errors import ConversationNotFoundError
from app.modules.chat.infrastructure.repositories import (
    ConversationRepository,
    MessageRepository,
    ToolCallRepository,
)
from app.modules.shared.public import TenantId
from app.modules.tenants.public import AgentConfigRepository, SqlTenantDirectory


async def _tenant_with_config(session: AsyncSession, slug: str) -> tuple[TenantId, UUID]:
    tenant = await SqlTenantDirectory(session).create(slug, slug)
    draft = await AgentConfigRepository(session).create_draft(tenant.id, {"assistant": {}})
    return tenant.id, draft.id


@pytest.fixture
def conversations(db_session: AsyncSession) -> ConversationRepository:
    return ConversationRepository(db_session)


@pytest.fixture
def messages(db_session: AsyncSession) -> MessageRepository:
    return MessageRepository(db_session)


@pytest.fixture
async def tenant_a(db_session: AsyncSession) -> tuple[TenantId, UUID]:
    return await _tenant_with_config(db_session, "tenant-a")


@pytest.fixture
async def tenant_b(db_session: AsyncSession) -> tuple[TenantId, UUID]:
    return await _tenant_with_config(db_session, "tenant-b")


@pytest.fixture
async def conversation(
    conversations: ConversationRepository, tenant_a: tuple[TenantId, UUID]
) -> Conversation:
    tenant_id, config_id = tenant_a
    return await conversations.create(tenant_id, config_id, Channel.WEB, "visitor-1")


def _text(conversation_id: UUID, text: str, client_message_id: UUID | None = None) -> NewMessage:
    return NewMessage(
        conversation_id=conversation_id,
        role=MessageRole.USER,
        status=MessageStatus.COMPLETED,
        content=text,
        input={"type": "text", "text": text},
        client_message_id=client_message_id,
    )


def _user_input(text: str) -> UserInput:
    return UserInput.model_validate({"type": "text", "text": text})


async def test_conversation_keeps_config_version(
    conversations: ConversationRepository,
    conversation: Conversation,
    tenant_a: tuple[TenantId, UUID],
) -> None:
    tenant_id, config_id = tenant_a

    found = await conversations.find(tenant_id, conversation.id)

    assert found == conversation
    assert (found.agent_config_id, found.channel, found.visitor_id) == (
        config_id,
        Channel.WEB,
        "visitor-1",
    )


async def test_conversations_are_isolated_by_tenant(
    conversations: ConversationRepository,
    conversation: Conversation,
    tenant_b: tuple[TenantId, UUID],
) -> None:
    other, _ = tenant_b

    assert await conversations.find(other, conversation.id) is None
    assert await conversations.list(other) == []
    assert await conversations.delete(other, conversation.id) is False
    assert await conversations.find(conversation.tenant_id, conversation.id) is not None


async def test_messages_are_isolated_by_tenant(
    messages: MessageRepository, conversation: Conversation, tenant_b: tuple[TenantId, UUID]
) -> None:
    other, _ = tenant_b
    message, _ = await messages.add_once(conversation.tenant_id, _text(conversation.id, "привет"))

    assert await messages.list_for(other, conversation.id) == []
    assert await messages.get(other, message.id) is None
    assert await messages.delete(other, message.id) is False
    assert [m.id for m in await messages.list_for(conversation.tenant_id, conversation.id)] == [
        message.id
    ]


async def test_history_is_ordered_by_creation_within_one_transaction(
    messages: MessageRepository, conversation: Conversation
) -> None:
    texts = ["первое", "второе", "третье"]
    for text in texts:
        await messages.add_once(conversation.tenant_id, _text(conversation.id, text))

    history = await messages.list_for(conversation.tenant_id, conversation.id)

    assert [m.content for m in history] == texts


async def test_messages_without_client_id_do_not_conflict(
    messages: MessageRepository, conversation: Conversation
) -> None:
    _, first = await messages.add_once(conversation.tenant_id, _text(conversation.id, "a"))
    _, second = await messages.add_once(conversation.tenant_id, _text(conversation.id, "a"))

    assert first and second


async def test_append_user_message_is_idempotent_by_client_message_id(
    conversations: ConversationRepository, messages: MessageRepository, conversation: Conversation
) -> None:
    tenant_id, client_id = conversation.tenant_id, uuid4()

    first, created = await append_user_message(
        tenant_id, conversation.id, client_id, _user_input("нужен крем"), conversations, messages
    )
    retry, created_again = await append_user_message(
        tenant_id, conversation.id, client_id, _user_input("другой текст"), conversations, messages
    )

    assert (created, created_again) == (True, False)
    assert retry == first
    assert (first.role, first.content, first.input) == (
        MessageRole.USER,
        "нужен крем",
        {"type": "text", "text": "нужен крем"},
    )
    assert len(await messages.list_for(tenant_id, conversation.id)) == 1


async def test_same_client_message_id_in_another_conversation_is_new_message(
    conversations: ConversationRepository,
    messages: MessageRepository,
    conversation: Conversation,
    tenant_a: tuple[TenantId, UUID],
) -> None:
    tenant_id, config_id = tenant_a
    other = await conversations.create(tenant_id, config_id, Channel.WEB, "visitor-2")
    client_id = uuid4()

    _, first = await append_user_message(
        tenant_id, conversation.id, client_id, _user_input("a"), conversations, messages
    )
    _, second = await append_user_message(
        tenant_id, other.id, client_id, _user_input("a"), conversations, messages
    )

    assert first and second


async def test_action_input_is_stored_without_text_content(
    conversations: ConversationRepository, messages: MessageRepository, conversation: Conversation
) -> None:
    action = UserInput.model_validate(
        {"type": "action", "action_id": "select_product", "payload": {"entity_id": "e_1"}}
    )

    message, _ = await append_user_message(
        conversation.tenant_id, conversation.id, uuid4(), action, conversations, messages
    )

    assert message.content == ""
    assert message.input == {
        "type": "action",
        "action_id": "select_product",
        "payload": {"entity_id": "e_1"},
    }


async def test_append_to_foreign_conversation_is_not_found(
    conversations: ConversationRepository,
    messages: MessageRepository,
    conversation: Conversation,
    tenant_b: tuple[TenantId, UUID],
) -> None:
    other, _ = tenant_b

    with pytest.raises(ConversationNotFoundError):
        await append_user_message(
            other, conversation.id, uuid4(), _user_input("a"), conversations, messages
        )
    assert await messages.list_for(conversation.tenant_id, conversation.id) == []


async def test_tool_calls_are_stored_in_order_and_isolated_by_tenant(
    db_session: AsyncSession,
    conversations: ConversationRepository,
    messages: MessageRepository,
) -> None:
    a, config_a = await _tenant_with_config(db_session, "tenant-a")
    b, _ = await _tenant_with_config(db_session, "tenant-b")
    conversation = await conversations.create(a, config_a, Channel.WEB, "v")
    answer, _ = await messages.add_once(
        a,
        NewMessage(
            conversation_id=conversation.id,
            role=MessageRole.ASSISTANT,
            status=MessageStatus.COMPLETED,
            content="Нашла",
        ),
    )
    calls = [
        ToolCallEntry("call_1", "search_catalog", {"query": "крем"}, {"items": [1]}, None, 12),
        ToolCallEntry("call_2", "get_entity", "{id:", None, {"code": "timeout", "message": "-"}, 0),
    ]
    tool_calls = ToolCallRepository(db_session)

    await tool_calls.add_many(a, answer.id, calls)

    assert await tool_calls.list_for(a, answer.id) == calls
    assert await tool_calls.list_for(b, answer.id) == []


async def test_state_update_is_isolated_by_tenant(
    db_session: AsyncSession, conversations: ConversationRepository
) -> None:
    a, config_a = await _tenant_with_config(db_session, "tenant-a")
    b, _ = await _tenant_with_config(db_session, "tenant-b")
    conversation = await conversations.create(a, config_a, Channel.WEB, "v")

    await conversations.update_state(b, conversation.id, {"slots": {"budget": 1}})
    await conversations.update_state(a, conversation.id, {"slots": {"budget": 3000}})

    found = await conversations.find(a, conversation.id)
    assert found is not None and found.state == {"slots": {"budget": 3000}}
    assert conversation.state == {}


async def test_replaced_message_is_hidden_and_marked_once(
    messages: MessageRepository, conversation: Conversation, tenant_b: tuple[TenantId, UUID]
) -> None:
    tenant_id = conversation.tenant_id
    question, _ = await messages.add_once(tenant_id, _text(conversation.id, "вопрос"))
    failed, _ = await messages.add_once(
        tenant_id,
        NewMessage(
            conversation_id=conversation.id,
            role=MessageRole.ASSISTANT,
            status=MessageStatus.FAILED,
            content="",
            error={"code": "llm_unavailable", "message": "нет модели", "retryable": True},
        ),
    )
    assert failed.error == {"code": "llm_unavailable", "message": "нет модели", "retryable": True}

    foreign = await messages.mark_replaced(tenant_b[0], failed.id, uuid4())
    first = await messages.mark_replaced(tenant_id, failed.id, uuid4())
    second = await messages.mark_replaced(tenant_id, failed.id, uuid4())

    assert (foreign, first, second) == (False, True, False)
    assert [m.id for m in await messages.list_for(tenant_id, conversation.id)] == [question.id]
