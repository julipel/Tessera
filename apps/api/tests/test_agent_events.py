"""Журнал событий хода (AgentEvent, P7-02) и его эндпоинт админки (ADR-0028)."""

import json
from collections.abc import AsyncIterator, Iterator
from typing import Any
from uuid import UUID, uuid4

import anyio
import pytest
from fastapi import FastAPI
from httpx import AsyncClient, Response
from pydantic import SecretStr
from sqlalchemy.ext.asyncio import AsyncSession

from app.contracts import AgentEventList, UserInput
from app.logs import TRACE_ID_HEADER
from app.modules.agent.public import (
    AgentEvent,
    AnswerDelta,
    ComponentEmitted,
    DialogStateUpdated,
    FinishReason,
    LLMError,
    SuggestionsOffered,
    ToolFinished,
    ToolStarted,
    TurnCompleted,
    Usage,
)
from app.modules.chat.api.router import get_turn_agent
from app.modules.chat.api.sse import sse_stream
from app.modules.chat.application.turns import TurnRegistry, start_turn
from app.modules.chat.domain.entities import TurnRequest
from app.modules.chat.domain.ports import TurnAgent
from app.modules.chat.infrastructure.repositories import (
    ConversationRepository,
    MessageRepository,
    TenantsAgentConfigs,
    ToolCallRepository,
)
from app.modules.memory.public import DialogState
from app.modules.observability.public import AgentEventRepository
from app.modules.shared.public import TenantId
from app.modules.tenants.public import (
    AgentConfigRepository,
    SqlTenantDirectory,
    WidgetKeyRepository,
    hash_widget_key,
)

URL = "/v1/conversations"
TOKEN = "adm-secret"
ADMIN = {"Authorization": f"Bearer {TOKEN}"}
TEXT = {"type": "text", "text": "Нужен подарок маме"}
CARD = {"type": "info_card", "title": "Доставка", "body_markdown": "1-2 дня"}


@pytest.fixture(autouse=True)
def admin_token(app: FastAPI) -> None:
    app.state.settings = app.state.settings.model_copy(update={"admin_api_token": SecretStr(TOKEN)})


class ScriptedAgent:
    def __init__(self, *turns: list[AgentEvent] | Exception) -> None:
        self.turns = list(turns)

    async def run_turn(self, request: TurnRequest) -> AsyncIterator[AgentEvent]:
        turn = self.turns.pop(0)
        if isinstance(turn, Exception):
            yield AnswerDelta("Сейчас ")
            raise turn
        for event in turn:
            yield event


@pytest.fixture
def use_agent(app: FastAPI) -> Iterator[Any]:
    def use(agent: TurnAgent) -> None:
        app.dependency_overrides[get_turn_agent] = lambda: agent

    yield use


async def _tenant(session: AsyncSession, slug: str) -> TenantId:
    tenant = await SqlTenantDirectory(session).create(slug, slug)
    await WidgetKeyRepository(session).add_key(tenant.id, hash_widget_key(f"wk_{slug}"), [])
    configs = AgentConfigRepository(session)
    draft = await configs.create_draft(
        tenant.id,
        {
            "assistant": {
                "name": slug,
                "greeting": "Привет!",
                "fallback_message": "Не получилось.",
            },
            "model": {"primary": {"provider": "openai", "name": "test-model"}},
            "limits": {},
            "prompt": {"tenant": "Ты — консультант."},
            "tools": {},
        },
    )
    await configs.activate(tenant.id, draft.id)
    return tenant.id


@pytest.fixture
async def shop(db_session: AsyncSession) -> TenantId:
    return await _tenant(db_session, "shop")


@pytest.fixture
async def conversation_id(db_client: AsyncClient, shop: TenantId) -> UUID:
    response = await db_client.post(
        URL, json={"visitor_id": "v-1"}, headers={"X-Widget-Key": "wk_shop"}
    )
    return UUID(response.json()["conversation_id"])


async def _send(
    client: AsyncClient, conversation_id: UUID, headers: dict[str, str] | None = None
) -> Response:
    return await client.post(
        f"{URL}/{conversation_id}/messages",
        json={"client_message_id": str(uuid4()), "input": TEXT},
        headers={"X-Widget-Key": "wk_shop", **(headers or {})},
    )


def _sse(response: Response) -> list[dict[str, Any]]:
    return [
        json.loads(line.removeprefix("data: "))
        for line in response.text.splitlines()
        if line.startswith("data: ")
    ]


async def _events(
    client: AsyncClient, tenant_id: UUID, headers: dict[str, str] = ADMIN, **filters: Any
) -> Response:
    params = {k: str(v) for k, v in filters.items()}
    return await client.get(f"/v1/admin/tenants/{tenant_id}/events", params=params, headers=headers)


async def _journal(client: AsyncClient, tenant_id: UUID, **filters: Any) -> list[dict[str, Any]]:
    response = await _events(client, tenant_id, **filters)
    assert response.status_code == 200
    return [
        e.model_dump(mode="json") for e in AgentEventList.model_validate(response.json()).events
    ]


async def test_turn_events_are_logged_in_order_with_trace_id(
    db_client: AsyncClient, shop: TenantId, conversation_id: UUID, use_agent: Any
) -> None:
    state = DialogState(active_scenario="gift", slots={"gift": {"budget": "3000"}})
    use_agent(
        ScriptedAgent(
            [
                AnswerDelta("Ищу "),
                ToolStarted("c1", "search_catalog", "Ищу в каталоге"),
                ToolFinished(
                    "c1", "search_catalog", ok=True, arguments={"q": "крем"}, content={"n": 1}
                ),
                DialogStateUpdated(state),
                ComponentEmitted(CARD),
                AnswerDelta("Вот "),
                AnswerDelta("вариант."),
                SuggestionsOffered(("Ещё",)),
                TurnCompleted(FinishReason.ANSWERED, Usage(12, 3), steps=2),
            ]
        )
    )

    response = await _send(db_client, conversation_id, {TRACE_ID_HEADER: "trace-p7"})
    sse = _sse(response)
    journal = await _journal(db_client, shop, conversation_id=conversation_id)

    assert [e["type"] for e in journal] == [
        "turn_started",
        "text",
        "tool_started",
        "tool_finished",
        "state_updated",
        "component",
        "text",
        "suggestions",
        "turn_completed",
        "turn_finished",
    ]
    assert [e["seq"] for e in journal] == list(range(1, 11))
    assert {(e["trace_id"], e["turn_id"]) for e in journal} == {("trace-p7", sse[0]["turn_id"])}
    payloads = [e["payload"] for e in journal]
    assert payloads[0]["input"] == TEXT and payloads[0]["retry_of"] is None
    assert payloads[1] == {"block_id": "b1", "text": "Ищу "}
    assert payloads[2] == {"tool_call_id": "c1", "name": "search_catalog"}
    # Результат инструмента — в ToolCall ответа, в журнале не дублируется.
    assert payloads[3] == {
        "tool_call_id": "c1",
        "name": "search_catalog",
        "ok": True,
        "arguments": {"q": "крем"},
        "duration_ms": 0,
    }
    assert payloads[4] == {"state": state.to_dict()}
    assert payloads[5] == {"block_id": "b2", "component": CARD}
    assert payloads[6] == {"block_id": "b3", "text": "Вот вариант."}
    assert payloads[7] == {"items": ["Ещё"]}
    assert payloads[8] == {
        "finish": "answered",
        "steps": 2,
        "usage": {"input_tokens": 12, "output_tokens": 3},
    }
    assert payloads[9] == {
        "status": "completed",
        "message_id": sse[-1]["message_id"],
        "error": None,
    }


async def test_generated_trace_id_matches_response_header(
    db_client: AsyncClient, shop: TenantId, conversation_id: UUID, use_agent: Any
) -> None:
    use_agent(ScriptedAgent([AnswerDelta("Да.")]))

    response = await _send(db_client, conversation_id)
    trace_id = response.headers[TRACE_ID_HEADER]
    journal = await _journal(db_client, shop, trace_id=trace_id)

    assert [e["type"] for e in journal] == ["turn_started", "text", "turn_finished"]


async def test_failed_turn_and_its_retry_are_logged(
    db_client: AsyncClient, shop: TenantId, conversation_id: UUID, use_agent: Any
) -> None:
    use_agent(ScriptedAgent(LLMError("503", retryable=True), [AnswerDelta("Готово.")]))

    failed = _sse(await _send(db_client, conversation_id))
    failed_id = failed[-1]["message_id"]
    retry = await db_client.post(
        f"{URL}/{conversation_id}/messages/{failed_id}/retry", headers={"X-Widget-Key": "wk_shop"}
    )
    retried = _sse(retry)

    first = await _journal(db_client, shop, turn_id=failed[0]["turn_id"])
    assert [(e["type"], e["payload"].get("text")) for e in first] == [
        ("turn_started", None),
        ("text", "Сейчас "),
        ("turn_finished", None),
    ]
    assert first[-1]["payload"] == {
        "status": "failed",
        "message_id": failed_id,
        "error": {
            "code": "llm_unavailable",
            "message": "модель сейчас недоступна",
            "retryable": True,
        },
    }
    second = await _journal(db_client, shop, turn_id=retried[0]["turn_id"])
    assert second[0]["payload"]["retry_of"] == failed_id
    assert second[-1]["payload"]["status"] == "completed"


class GatedAgent:
    def __init__(self) -> None:
        self.first_sent = anyio.Event()

    async def run_turn(self, request: TurnRequest) -> AsyncIterator[AgentEvent]:
        yield AnswerDelta("Подбираю ")
        self.first_sent.set()
        await anyio.sleep_forever()
        yield AnswerDelta("не дойдёт")


async def test_cancelled_turn_is_logged_as_interrupted(
    db_session: AsyncSession, shop: TenantId, conversation_id: UUID
) -> None:
    agent, registry = GatedAgent(), TurnRegistry()
    turn = await start_turn(
        shop,
        conversation_id,
        uuid4(),
        UserInput.model_validate(TEXT),
        ConversationRepository(db_session),
        MessageRepository(db_session),
        ToolCallRepository(db_session),
        TenantsAgentConfigs(db_session),
        agent,
        db_session.commit,
        registry,
        agent_events=AgentEventRepository(db_session),
        trace_id="trace-cancel",
    )

    async def cancel_after_first_chunk() -> None:
        await agent.first_sent.wait()
        await registry.cancel(shop, conversation_id, turn.turn_id)

    async with anyio.create_task_group() as tg:
        tg.start_soon(cancel_after_first_chunk)
        async for _ in sse_stream(turn):
            pass

    journal = await AgentEventRepository(db_session).find(shop, turn_id=turn.turn_id)
    assert [(e.type, e.payload.get("text"), e.payload.get("status")) for e in journal] == [
        ("turn_started", None, None),
        ("text", "Подбираю ", None),
        ("turn_finished", None, "interrupted"),
    ]


async def test_admin_events_auth_and_filters(
    app: FastAPI,
    db_client: AsyncClient,
    db_session: AsyncSession,
    shop: TenantId,
    conversation_id: UUID,
    use_agent: Any,
) -> None:
    use_agent(ScriptedAgent([AnswerDelta("Да.")]))
    await _send(db_client, conversation_id)
    other = await _tenant(db_session, "other")

    assert (
        await _events(db_client, shop, headers={}, conversation_id=conversation_id)
    ).status_code == 401
    wrong = {"Authorization": "Bearer nope"}
    assert (
        await _events(db_client, shop, headers=wrong, conversation_id=conversation_id)
    ).status_code == 401
    assert (await _events(db_client, shop)).status_code == 422
    # Чужой тенант событий диалога не видит.
    assert await _journal(db_client, other, conversation_id=conversation_id) == []

    app.state.settings = app.state.settings.model_copy(update={"admin_api_token": None})
    disabled = await _events(db_client, shop, conversation_id=conversation_id)
    assert disabled.status_code == 404
