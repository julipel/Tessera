"""Диалоги в админке (P8-04a, ADR-0038): список страницами, разбор с заменёнными ответами
и вызовами инструментов, оценка ответа сотрудником, изоляция тенантов."""

from collections.abc import Awaitable, Callable
from datetime import datetime
from uuid import UUID, uuid4

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.contracts import ConversationDetail, ConversationList, MessageRating
from app.modules.chat.application.review import decode_cursor, encode_cursor
from app.modules.chat.domain.entities import (
    Channel,
    ChatMessage,
    MessageRole,
    MessageStatus,
    NewMessage,
    ToolCallEntry,
)
from app.modules.chat.domain.errors import InvalidCursorError
from app.modules.chat.domain.review import PageCursor
from app.modules.chat.infrastructure.repositories import (
    ConversationRepository,
    MessageRepository,
    ToolCallRepository,
)
from app.modules.shared.public import AdminRole, TenantId
from app.modules.tenants.public import AgentConfigRepository, SqlTenantDirectory

AdminLogin = Callable[..., Awaitable[dict[str, str]]]  # фикстура admin_login из conftest


class Shop:
    def __init__(self, session: AsyncSession, tenant_id: TenantId, config_id: UUID) -> None:
        self.session = session
        self.id = tenant_id
        self.config_id = config_id

    async def conversation(self, *texts: str) -> UUID:
        """Диалог: на каждый текст клиента — ответ ассистента."""
        conversation = await ConversationRepository(self.session).create(
            self.id, self.config_id, Channel.WEB, "visitor", language="ru"
        )
        for text in texts:
            await self.user(conversation.id, text)
            await self.assistant(conversation.id, f"ответ на «{text}»")
        return conversation.id

    async def user(self, conversation_id: UUID, text: str) -> ChatMessage:
        message, _ = await MessageRepository(self.session).add_once(
            self.id,
            NewMessage(
                conversation_id=conversation_id,
                role=MessageRole.USER,
                status=MessageStatus.COMPLETED,
                content=text,
                input={"type": "text", "text": text},
                client_message_id=uuid4(),
            ),
        )
        return message

    async def assistant(
        self,
        conversation_id: UUID,
        text: str,
        status: MessageStatus = MessageStatus.COMPLETED,
        error: dict[str, object] | None = None,
    ) -> ChatMessage:
        message, _ = await MessageRepository(self.session).add_once(
            self.id,
            NewMessage(
                conversation_id=conversation_id,
                role=MessageRole.ASSISTANT,
                status=status,
                content=text,
                blocks=({"type": "text", "block_id": "b1", "text": text},),
                error=error,
            ),
        )
        return message


async def _shop(session: AsyncSession, slug: str) -> Shop:
    tenant = await SqlTenantDirectory(session).create(slug, slug)
    draft = await AgentConfigRepository(session).create_draft(tenant.id, {"assistant": {}})
    return Shop(session, tenant.id, draft.id)


@pytest.fixture
async def shop(db_session: AsyncSession) -> Shop:
    return await _shop(db_session, "shop")


@pytest.fixture
async def viewer(admin_login: AdminLogin, shop: Shop) -> dict[str, str]:
    return await admin_login("viewer@example.com", roles={shop.id: AdminRole.VIEWER})


def _url(shop: Shop, suffix: str = "") -> str:
    return f"/v1/admin/tenants/{shop.id}/conversations{suffix}"


def _rating_url(shop: Shop, conversation_id: UUID, message_id: UUID) -> str:
    return _url(shop, f"/{conversation_id}/messages/{message_id}/rating")


async def _messages(shop: Shop, conversation_id: UUID) -> list[ChatMessage]:
    return list(await MessageRepository(shop.session).list_for(shop.id, conversation_id))


# --- доступ ---


async def test_requires_session_and_role(
    db_client: AsyncClient, admin_login: AdminLogin, shop: Shop
) -> None:
    assert (await db_client.get(_url(shop))).status_code == 401
    other = await _shop(shop.session, "other")
    stranger = await admin_login("stranger@example.com", roles={other.id: AdminRole.EDITOR})
    assert (await db_client.get(_url(shop), headers=stranger)).status_code == 403


# --- список ---


async def test_list_newest_first_with_summary(
    db_client: AsyncClient, shop: Shop, viewer: dict[str, str]
) -> None:
    empty = await shop.conversation()
    long_text = "Подберите крем " + "очень " * 60
    first = await shop.conversation(long_text, "А дешевле?")

    response = await db_client.get(_url(shop), headers=viewer)

    assert response.status_code == 200, response.text
    page = ConversationList.model_validate(response.json())
    assert [c.id for c in page.conversations] == [first, empty]
    assert page.next_before is None
    item = page.conversations[0]
    assert item.messages == 4
    assert item.preview == long_text[:200]
    assert item.language == "ru"
    assert item.agent_config_id == shop.config_id
    assert item.agent_config_version == 1
    assert item.last_message_at is not None and item.last_message_at >= item.created_at
    assert item.ratings.good == item.ratings.bad == 0
    blank = page.conversations[1]
    assert (blank.messages, blank.preview, blank.last_message_at) == (0, None, None)


async def test_list_pages_with_cursor(
    db_client: AsyncClient, shop: Shop, viewer: dict[str, str]
) -> None:
    ids = [await shop.conversation(f"вопрос {i}") for i in range(5)]

    seen: list[UUID] = []
    before: str | None = None
    for _ in range(3):
        params = {"limit": "2"} | ({"before": before} if before else {})
        page = ConversationList.model_validate(
            (await db_client.get(_url(shop), params=params, headers=viewer)).json()
        )
        seen += [c.id for c in page.conversations]
        before = page.next_before
        if before is None:
            break

    assert seen == ids[::-1]
    assert before is None


async def test_list_rejects_bad_cursor(
    db_client: AsyncClient, shop: Shop, viewer: dict[str, str]
) -> None:
    response = await db_client.get(_url(shop), params={"before": "мусор"}, headers=viewer)
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid_input"


def test_cursor_round_trip() -> None:
    cursor = PageCursor(datetime.fromisoformat("2026-10-11T10:00:00.123456+00:00"), uuid4())
    assert decode_cursor(encode_cursor(cursor)) == cursor
    with pytest.raises(InvalidCursorError):
        decode_cursor(encode_cursor(cursor)[:-3] + "!!!")


async def test_list_filters_by_rating(
    db_client: AsyncClient, shop: Shop, viewer: dict[str, str]
) -> None:
    good = await shop.conversation("хорошо")
    bad = await shop.conversation("плохо", "ещё")
    await shop.conversation("без оценки")
    good_answer = (await _messages(shop, good))[1]
    bad_answers = [m for m in await _messages(shop, bad) if m.role is MessageRole.ASSISTANT]
    await db_client.put(
        _rating_url(shop, good, good_answer.id), json={"rating": "good"}, headers=viewer
    )
    for m in bad_answers:
        await db_client.put(_rating_url(shop, bad, m.id), json={"rating": "bad"}, headers=viewer)

    page = ConversationList.model_validate(
        (await db_client.get(_url(shop), params={"rating": "bad"}, headers=viewer)).json()
    )

    assert [c.id for c in page.conversations] == [bad]
    assert (page.conversations[0].ratings.good, page.conversations[0].ratings.bad) == (0, 2)
    every = ConversationList.model_validate(
        (await db_client.get(_url(shop), headers=viewer)).json()
    )
    assert {c.id: c.ratings.good for c in every.conversations}[good] == 1


# --- разбор ---


async def test_detail_shows_replaced_answers_and_tool_calls(
    db_client: AsyncClient, shop: Shop, viewer: dict[str, str]
) -> None:
    conversation_id = await shop.conversation()
    question = await shop.user(conversation_id, "Есть крем для сухой кожи?")
    failed = await shop.assistant(
        conversation_id,
        "",
        status=MessageStatus.FAILED,
        error={"code": "llm_unavailable", "message": "модель недоступна", "retryable": True},
    )
    answer = await shop.assistant(conversation_id, "Да, вот варианты")
    await MessageRepository(shop.session).mark_replaced(shop.id, failed.id, answer.id)
    await ToolCallRepository(shop.session).add_many(
        shop.id,
        answer.id,
        [
            ToolCallEntry("call-1", "search_catalog", {"query": "крем"}, {"items": []}, None, 120),
            ToolCallEntry(
                "call-2", "show_entities", "{не json", None, {"code": "bad_args", "message": "x"}, 3
            ),
        ],
    )

    response = await db_client.get(_url(shop, f"/{conversation_id}"), headers=viewer)

    assert response.status_code == 200, response.text
    body = response.json()
    detail = ConversationDetail.model_validate(body)
    assert [m.message_id for m in detail.messages] == [question.id, failed.id, answer.id]
    assert detail.messages[1].replaced_by == answer.id
    assert detail.messages[1].error is not None
    assert detail.messages[1].error.code == "llm_unavailable"
    calls = detail.messages[2].tool_calls
    assert [(c.name, c.arguments, c.duration_ms) for c in calls] == [
        ("search_catalog", {"query": "крем"}, 120),
        ("show_entities", "{не json", 3),
    ]
    assert calls[1].error is not None and calls[1].error.code == "bad_args"
    assert detail.conversation.messages == 3
    assert detail.state == {} and detail.history_summary is None
    # Поля, которых у сообщения нет, опускаются; replaced_by и rating — всегда есть.
    user_json, answer_json = body["messages"][0], body["messages"][2]
    assert "error" not in user_json and user_json["input"]["text"] == question.content
    assert "input" not in answer_json and "error" not in answer_json
    assert answer_json["replaced_by"] is None and answer_json["rating"] is None


async def test_detail_of_unknown_or_foreign_conversation_is_404(
    db_client: AsyncClient, shop: Shop, viewer: dict[str, str]
) -> None:
    other = await _shop(shop.session, "other")
    foreign = await other.conversation("чужой")

    for conversation_id in (uuid4(), foreign):
        response = await db_client.get(_url(shop, f"/{conversation_id}"), headers=viewer)
        assert response.status_code == 404
        assert response.json()["error"]["code"] == "not_found"


# --- оценка ---


async def test_rate_answer_replace_and_clear(
    db_client: AsyncClient, admin_login: AdminLogin, shop: Shop, viewer: dict[str, str]
) -> None:
    conversation_id = await shop.conversation("Сколько стоит доставка?")
    answer = (await _messages(shop, conversation_id))[1]
    url = _rating_url(shop, conversation_id, answer.id)

    response = await db_client.put(
        url, json={"rating": "bad", "comment": "  выдумал цену  "}, headers=viewer
    )

    assert response.status_code == 200, response.text
    first = MessageRating.model_validate(response.json())
    assert (first.rating, first.comment, first.rated_by) == (
        "bad",
        "выдумал цену",
        "viewer@example.com",
    )

    editor = await admin_login("editor@example.com", roles={shop.id: AdminRole.EDITOR})
    second = MessageRating.model_validate(
        (await db_client.put(url, json={"rating": "good", "comment": " "}, headers=editor)).json()
    )
    assert (second.rating, second.comment, second.rated_by) == (
        "good",
        None,
        "editor@example.com",
    )
    assert second.updated_at >= first.updated_at

    detail = ConversationDetail.model_validate(
        (await db_client.get(_url(shop, f"/{conversation_id}"), headers=viewer)).json()
    )
    assert detail.messages[1].rating == second
    assert (detail.conversation.ratings.good, detail.conversation.ratings.bad) == (1, 0)

    assert (await db_client.delete(url, headers=viewer)).status_code == 204
    assert (await db_client.delete(url, headers=viewer)).status_code == 204
    detail = ConversationDetail.model_validate(
        (await db_client.get(_url(shop, f"/{conversation_id}"), headers=viewer)).json()
    )
    assert detail.messages[1].rating is None


async def test_rate_rejects_user_message_and_long_comment(
    db_client: AsyncClient, shop: Shop, viewer: dict[str, str]
) -> None:
    conversation_id = await shop.conversation("Привет")
    question, answer = await _messages(shop, conversation_id)

    response = await db_client.put(
        _rating_url(shop, conversation_id, question.id), json={"rating": "good"}, headers=viewer
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid_input"

    response = await db_client.put(
        _rating_url(shop, conversation_id, answer.id),
        json={"rating": "good", "comment": "x" * 2001},
        headers=viewer,
    )
    assert response.status_code == 422

    response = await db_client.put(
        _rating_url(shop, conversation_id, answer.id), json={"rating": "meh"}, headers=viewer
    )
    assert response.status_code == 422


async def test_rate_foreign_or_mismatched_message_is_404(
    db_client: AsyncClient, admin_login: AdminLogin, shop: Shop, viewer: dict[str, str]
) -> None:
    other = await _shop(shop.session, "other")
    foreign = await other.conversation("чужой")
    foreign_answer = (await _messages(other, foreign))[1]
    mine = await shop.conversation("свой")
    my_answer = (await _messages(shop, mine))[1]
    another = await shop.conversation("второй")

    cases = [
        (foreign, foreign_answer.id),  # чужой тенант по пути shop
        (mine, foreign_answer.id),  # сообщение чужого тенанта в своём диалоге
        (another, my_answer.id),  # сообщение из другого диалога
        (mine, uuid4()),
    ]
    for conversation_id, message_id in cases:
        url = _rating_url(shop, conversation_id, message_id)
        assert (await db_client.put(url, json={"rating": "bad"}, headers=viewer)).status_code == 404
        assert (await db_client.delete(url, headers=viewer)).status_code == 404

    # Оценка чужого ответа его тенантом не видна в списке shop.
    other_viewer = await admin_login("other@example.com", roles={other.id: AdminRole.VIEWER})
    put = await db_client.put(
        _rating_url(other, foreign, foreign_answer.id), json={"rating": "bad"}, headers=other_viewer
    )
    assert put.status_code == 200
    page = ConversationList.model_validate(
        (await db_client.get(_url(shop), params={"rating": "bad"}, headers=viewer)).json()
    )
    assert page.conversations == []
