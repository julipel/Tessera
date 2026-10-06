"""POST /v1/conversations и GET /v1/conversations/{id}/messages."""

from typing import Any
from uuid import UUID, uuid4

import pytest
import structlog
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.contracts import CreateConversationResponse, HttpError, MessageHistory, UserInput
from app.logs import TRACE_ID_HEADER
from app.modules.chat.application.conversations import append_user_message
from app.modules.chat.domain.entities import MessageRole, MessageStatus, NewMessage
from app.modules.chat.infrastructure.repositories import (
    ConversationRepository,
    MessageRepository,
)
from app.modules.shared.public import TenantId
from app.modules.tenants.public import (
    AgentConfigRepository,
    SqlTenantDirectory,
    WidgetKeyRepository,
    hash_widget_key,
)

URL = "/v1/conversations"
CARD: dict[str, Any] = {
    "type": "product_card",
    "entity_id": "e_1",
    "title": "Крем",
    "price": {"amount": 4990, "currency": "SEK"},
}


async def _tenant(
    session: AsyncSession, slug: str, *, active: bool = True, **assistant: Any
) -> TenantId:
    tenant = await SqlTenantDirectory(session).create(slug, slug)
    await WidgetKeyRepository(session).add_key(tenant.id, hash_widget_key(f"wk_{slug}"), [])
    if active:
        configs = AgentConfigRepository(session)
        config = {
            "assistant": {"name": slug, "greeting": "", "fallback_message": "", **assistant},
            "model": {"primary": {"provider": "openai", "name": "m"}},
            "limits": {},
            "prompt": {"tenant": "t"},
            "tools": {},
        }
        draft = await configs.create_draft(tenant.id, config)
        await configs.activate(tenant.id, draft.id)
    return tenant.id


def _key(slug: str) -> dict[str, str]:
    return {"X-Widget-Key": f"wk_{slug}"}


def _error_code(body: Any) -> str:
    return HttpError.model_validate(body).error.code


@pytest.fixture
async def shop(db_session: AsyncSession) -> TenantId:
    return await _tenant(db_session, "shop")


async def _create(client: AsyncClient, slug: str = "shop", **body: Any) -> UUID:
    response = await client.post(URL, json={"visitor_id": "v-1", **body}, headers=_key(slug))
    assert response.status_code == 201, response.text
    return CreateConversationResponse.model_validate(response.json()).conversation_id


async def test_create_remembers_active_config_version(
    db_client: AsyncClient, db_session: AsyncSession, shop: TenantId
) -> None:
    conversation_id = await _create(db_client)

    found = await ConversationRepository(db_session).find(shop, conversation_id)
    active = await AgentConfigRepository(db_session).get_active(shop)
    assert found is not None and active is not None
    assert (found.agent_config_id, found.channel, found.visitor_id) == (active.id, "web", "v-1")


@pytest.mark.parametrize(
    ("assistant", "locale", "expected"),
    [
        ({}, "en-US", "en"),
        ({}, "sv_SE", "sv"),
        ({}, "de-DE", "ru"),  # не поддерживается — default_language
        ({}, None, "ru"),
        ({"default_language": "en"}, "fi", "en"),
        ({"language": "sv"}, "en-US", "sv"),  # фиксированный язык важнее locale
    ],
)
async def test_create_fixes_conversation_language(
    db_client: AsyncClient,
    db_session: AsyncSession,
    assistant: dict[str, Any],
    locale: str | None,
    expected: str,
) -> None:
    tenant = await _tenant(db_session, "intl", **assistant)
    body = {"locale": locale} if locale else {}

    conversation_id = await _create(db_client, "intl", **body)

    found = await ConversationRepository(db_session).find(tenant, conversation_id)
    assert found is not None and found.language == expected


async def test_create_without_key_is_401(db_client: AsyncClient, shop: TenantId) -> None:
    response = await db_client.post(URL, json={"visitor_id": "v-1"})

    assert response.status_code == 401
    assert _error_code(response.json()) == "unauthorized"


async def test_create_without_active_config_is_404(
    db_client: AsyncClient, db_session: AsyncSession
) -> None:
    await _tenant(db_session, "empty", active=False)

    response = await db_client.post(URL, json={"visitor_id": "v-1"}, headers=_key("empty"))

    assert response.status_code == 404
    assert _error_code(response.json()) == "not_found"


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"visitor_id": ""},
        {"visitor_id": "v-1", "channel": "telegram"},
        {"visitor_id": "v-1", "locale": ""},
    ],
)
async def test_create_with_invalid_body_is_422(
    db_client: AsyncClient, shop: TenantId, body: dict[str, Any]
) -> None:
    response = await db_client.post(URL, json=body, headers=_key("shop"))

    assert response.status_code == 422
    assert _error_code(response.json()) == "invalid_input"


async def test_history_returns_messages_in_order(
    db_client: AsyncClient, db_session: AsyncSession, shop: TenantId
) -> None:
    conversation_id = await _create(db_client)
    conversations, messages = ConversationRepository(db_session), MessageRepository(db_session)
    user_input = UserInput.model_validate({"type": "text", "text": "Нужен крем"})
    await append_user_message(shop, conversation_id, uuid4(), user_input, conversations, messages)
    blocks = (
        {"type": "text", "block_id": "b1", "text": "Вот вариант:"},
        {"type": "component", "block_id": "b2", "component": CARD},
    )
    await messages.add_once(
        shop,
        NewMessage(
            conversation_id=conversation_id,
            role=MessageRole.ASSISTANT,
            status=MessageStatus.INTERRUPTED,
            content="Вот вариант:",
            blocks=blocks,
        ),
    )

    response = await db_client.get(f"{URL}/{conversation_id}/messages", headers=_key("shop"))

    assert response.status_code == 200
    history = MessageHistory.model_validate(response.json())
    assert history.conversation_id == conversation_id
    user, assistant = history.messages
    assert (user.role, user.status, user.blocks) == ("user", "completed", [])
    assert user.input is not None
    assert user.input.model_dump() == {"type": "text", "text": "Нужен крем"}
    assert (assistant.role, assistant.status) == ("assistant", "interrupted")
    assert [b.root.block_id for b in assistant.blocks] == ["b1", "b2"]
    assert "input" not in response.json()["messages"][1]


async def test_history_of_empty_conversation(db_client: AsyncClient, shop: TenantId) -> None:
    conversation_id = await _create(db_client)

    response = await db_client.get(f"{URL}/{conversation_id}/messages", headers=_key("shop"))

    assert response.json() == {"conversation_id": str(conversation_id), "messages": []}


async def test_history_of_foreign_or_missing_conversation_is_404(
    db_client: AsyncClient, db_session: AsyncSession, shop: TenantId
) -> None:
    conversation_id = await _create(db_client)
    await _tenant(db_session, "other")

    foreign = await db_client.get(f"{URL}/{conversation_id}/messages", headers=_key("other"))
    missing = await db_client.get(f"{URL}/{uuid4()}/messages", headers=_key("shop"))

    for response in (foreign, missing):
        assert response.status_code == 404
        assert _error_code(response.json()) == "conversation_not_found"


async def test_history_with_malformed_id_is_422(db_client: AsyncClient, shop: TenantId) -> None:
    response = await db_client.get(f"{URL}/not-a-uuid/messages", headers=_key("shop"))

    assert response.status_code == 422
    assert _error_code(response.json()) == "invalid_input"


async def test_create_logs_tenant_and_conversation(db_client: AsyncClient, shop: TenantId) -> None:
    with structlog.testing.capture_logs(
        processors=[structlog.contextvars.merge_contextvars]
    ) as logs:
        response = await db_client.post(
            URL, json={"visitor_id": "v-1"}, headers={**_key("shop"), TRACE_ID_HEADER: "trace-c"}
        )

    conversation_id = response.json()["conversation_id"]
    [entry] = [e for e in logs if e["event"] == "conversation_created"]
    assert (entry["tenant_id"], entry["conversation_id"], entry["trace_id"]) == (
        str(shop),
        conversation_id,
        "trace-c",
    )
