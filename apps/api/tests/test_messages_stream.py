"""POST /v1/conversations/{id}/messages: события агента → SSE-стрим хода (docs/contracts.md §2)."""

import asyncio
import json
from collections.abc import AsyncIterator, Iterator, Mapping
from typing import Any
from uuid import UUID, uuid4

import anyio
import pytest
import structlog
from fastapi import FastAPI
from httpx import AsyncClient, Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.contracts import Event, HttpError, MessageHistory, UserInput
from app.logs import TRACE_ID_HEADER
from app.modules.agent.public import (
    AgentEvent,
    AnswerDelta,
    ComponentEmitted,
    DialogStateUpdated,
    FakeLLM,
    FakeReply,
    FinishReason,
    LLMError,
    RegistryToolExecutor,
    ToolCall,
    ToolFinished,
    ToolStarted,
    TurnCompleted,
    Usage,
    UserMessage,
)
from app.modules.chat.api.router import get_turn_agent
from app.modules.chat.api.sse import sse_stream
from app.modules.chat.application.turns import TurnRegistry, start_turn
from app.modules.chat.domain.entities import ToolCallEntry, TurnRequest
from app.modules.chat.domain.ports import TurnAgent
from app.modules.chat.infrastructure.loop_agent import LoopTurnAgent, builtin_turn_agent
from app.modules.chat.infrastructure.repositories import (
    ConversationRepository,
    MessageRepository,
    TenantsAgentConfigs,
    ToolCallRepository,
)
from app.modules.leads.public import LeadRepository
from app.modules.memory.public import DialogState
from app.modules.shared.public import TenantId
from app.modules.tenants.public import (
    AgentConfigRepository,
    SqlTenantDirectory,
    WidgetKeyRepository,
    hash_widget_key,
)
from app.modules.tools.public import ToolRegistry, builtin_tools, suggest_replies_tool

URL = "/v1/conversations"
TEXT = {"type": "text", "text": "Нужен подарок маме"}
ANSWER = "Подскажите, какой бюджет?"


def _config(slug: str) -> dict[str, Any]:
    return {
        "assistant": {"name": slug, "greeting": "Привет!", "fallback_message": "Не получилось."},
        "model": {"primary": {"provider": "openai", "name": "test-model"}},
        "limits": {},
        "prompt": {"tenant": f"Ты — консультант {slug}."},
        "tools": {},
    }


async def _tenant(
    session: AsyncSession, slug: str, config: dict[str, Any] | None = None
) -> TenantId:
    tenant = await SqlTenantDirectory(session).create(slug, slug)
    await WidgetKeyRepository(session).add_key(tenant.id, hash_widget_key(f"wk_{slug}"), [])
    configs = AgentConfigRepository(session)
    draft = await configs.create_draft(tenant.id, config or _config(slug))
    await configs.activate(tenant.id, draft.id)
    return tenant.id


def _key(slug: str) -> dict[str, str]:
    return {"X-Widget-Key": f"wk_{slug}"}


def _error_code(response: Response) -> str:
    return HttpError.model_validate(response.json()).error.code


def _parse_sse(body: str) -> list[tuple[str, Event]]:
    """Пары (поле `event:`, событие из `data:`) в порядке стрима."""
    parsed = []
    for frame in body.split("\n\n"):
        if not frame.strip():
            continue
        fields = dict(line.split(": ", 1) for line in frame.split("\n"))
        parsed.append((fields["event"], Event.model_validate(json.loads(fields["data"]))))
    return parsed


class ScriptedAgent:
    """Отдаёт заданные события; по умолчанию — ответ ANSWER кусками по словам."""

    def __init__(self, events: list[AgentEvent] | None = None) -> None:
        self.events = events or [
            *(AnswerDelta(w) for w in ("Подскажите, ", "какой ", "бюджет?")),
            TurnCompleted(FinishReason.ANSWERED, Usage(12, 3), steps=1),
        ]
        self.requests: list[TurnRequest] = []

    async def run_turn(self, request: TurnRequest) -> AsyncIterator[AgentEvent]:
        self.requests.append(request)
        for event in self.events:
            yield event


class FailingAgent:
    def __init__(self, error: Exception | None = None) -> None:
        self.error = error or RuntimeError("агент упал")

    async def run_turn(self, request: TurnRequest) -> AsyncIterator[AgentEvent]:
        yield AnswerDelta("Сейчас ")
        raise self.error


@pytest.fixture
def use_agent(app: FastAPI) -> Iterator[Any]:
    def use(agent: TurnAgent) -> None:
        app.dependency_overrides[get_turn_agent] = lambda: agent

    use(ScriptedAgent())
    yield use


@pytest.fixture
async def shop(db_session: AsyncSession) -> TenantId:
    return await _tenant(db_session, "shop")


@pytest.fixture
async def conversation_id(db_client: AsyncClient, shop: TenantId, use_agent: Any) -> UUID:
    response = await db_client.post(URL, json={"visitor_id": "v-1"}, headers=_key("shop"))
    return UUID(response.json()["conversation_id"])


async def _send(
    client: AsyncClient,
    conversation_id: UUID,
    user_input: dict[str, Any] = TEXT,
    client_message_id: UUID | None = None,
    slug: str = "shop",
) -> Response:
    return await client.post(
        f"{URL}/{conversation_id}/messages",
        json={"client_message_id": str(client_message_id or uuid4()), "input": user_input},
        headers=_key(slug),
    )


async def _history(client: AsyncClient, conversation_id: UUID) -> MessageHistory:
    response = await client.get(f"{URL}/{conversation_id}/messages", headers=_key("shop"))
    return MessageHistory.model_validate(response.json())


async def test_stream_follows_protocol(db_client: AsyncClient, conversation_id: UUID) -> None:
    response = await _send(db_client, conversation_id)

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    events = _parse_sse(response.text)
    types = [e.root.type for _, e in events]
    assert types[0] == "turn_started"
    assert types[-2:] == ["text_done", "done"]
    assert set(types[1:-2]) == {"text_delta"} and len(types) > 4
    assert all(name == e.root.type for name, e in events)
    assert [e.root.seq for _, e in events] == list(range(1, len(events) + 1))
    first, *rest = [e.root for _, e in events]
    assert first.message_id is None
    assert {(e.conversation_id, e.turn_id, e.message_id) for e in rest} == {
        (str(conversation_id), first.turn_id, rest[0].message_id)
    }
    deltas = [e.root.data for _, e in events if e.root.type == "text_delta"]
    assert "".join(d.delta for d in deltas) == ANSWER
    assert {d.block_id for d in deltas} == {"b1"}
    assert events[-1][1].model_dump()["data"] == {
        "status": "completed",
        "usage": {"input_tokens": 12, "output_tokens": 3, "tool_calls": 0},
    }


async def test_turn_is_saved_to_history(db_client: AsyncClient, conversation_id: UUID) -> None:
    events = _parse_sse((await _send(db_client, conversation_id)).text)

    user, assistant = (await _history(db_client, conversation_id)).messages
    assert user.input is not None and user.input.model_dump() == TEXT
    assert (assistant.role, assistant.status) == ("assistant", "completed")
    assert str(assistant.message_id) == events[-1][1].root.message_id
    assert [b.root.model_dump() for b in assistant.blocks] == [
        {"type": "text", "block_id": "b1", "text": ANSWER}
    ]


async def test_repeated_client_message_id_is_409(
    db_client: AsyncClient, conversation_id: UUID
) -> None:
    client_message_id = uuid4()
    first = await _send(db_client, conversation_id, client_message_id=client_message_id)
    retry = await _send(db_client, conversation_id, client_message_id=client_message_id)

    assert first.status_code == 200
    assert retry.status_code == 409
    assert _error_code(retry) == "duplicate_message"
    roles = [m.role for m in (await _history(db_client, conversation_id)).messages]
    assert roles == ["user", "assistant"]


async def test_send_without_key_is_401(db_client: AsyncClient, conversation_id: UUID) -> None:
    response = await db_client.post(
        f"{URL}/{conversation_id}/messages",
        json={"client_message_id": str(uuid4()), "input": TEXT},
    )

    assert response.status_code == 401
    assert _error_code(response) == "unauthorized"


async def test_send_to_foreign_or_missing_conversation_is_404(
    db_client: AsyncClient, db_session: AsyncSession, conversation_id: UUID
) -> None:
    await _tenant(db_session, "other")

    foreign = await _send(db_client, conversation_id, slug="other")
    missing = await _send(db_client, uuid4())

    for response in (foreign, missing):
        assert response.status_code == 404
        assert _error_code(response) == "conversation_not_found"
    assert (await _history(db_client, conversation_id)).messages == []


@pytest.mark.parametrize(
    "body",
    [
        {"input": TEXT},
        {"client_message_id": "not-a-uuid", "input": TEXT},
        {"client_message_id": str(uuid4()), "input": {"type": "text", "text": ""}},
        {"client_message_id": str(uuid4()), "input": {"type": "voice"}},
    ],
)
async def test_send_with_invalid_body_is_422(
    db_client: AsyncClient, conversation_id: UUID, body: dict[str, Any]
) -> None:
    response = await db_client.post(
        f"{URL}/{conversation_id}/messages", json=body, headers=_key("shop")
    )

    assert response.status_code == 422
    assert _error_code(response) == "invalid_input"


async def test_agent_failure_ends_turn_with_error(
    db_client: AsyncClient, conversation_id: UUID, use_agent: Any
) -> None:
    use_agent(FailingAgent())

    events = [e.model_dump() for _, e in _parse_sse((await _send(db_client, conversation_id)).text)]

    assert [e["type"] for e in events] == ["turn_started", "text_delta", "error", "done"]
    assert events[2]["data"] == {
        "code": "internal",
        "message": "не удалось ответить",
        "retryable": False,
    }
    assert events[3]["data"]["status"] == "failed"
    _, assistant = (await _history(db_client, conversation_id)).messages
    assert assistant.status == "failed"
    assert [b.root.model_dump()["text"] for b in assistant.blocks] == ["Сейчас "]


async def test_turn_logs_tenant_conversation_and_trace(
    db_client: AsyncClient, conversation_id: UUID, shop: TenantId
) -> None:
    with structlog.testing.capture_logs(
        processors=[structlog.contextvars.merge_contextvars]
    ) as logs:
        await db_client.post(
            f"{URL}/{conversation_id}/messages",
            json={"client_message_id": str(uuid4()), "input": TEXT},
            headers={**_key("shop"), TRACE_ID_HEADER: "trace-t"},
        )

    [entry] = [e for e in logs if e["event"] == "turn_finished"]
    assert (entry["tenant_id"], entry["conversation_id"], entry["trace_id"]) == (
        str(shop),
        str(conversation_id),
        "trace-t",
    )
    assert entry["status"] == "completed" and entry["turn_id"]


class GatedAgent:
    """Отдаёт первый кусок и ждёт, пока тест не оборвёт стрим или не отменит ход."""

    def __init__(self) -> None:
        self.first_sent = anyio.Event()
        self.turn_id: UUID | None = None

    async def run_turn(self, request: TurnRequest) -> AsyncIterator[AgentEvent]:
        self.turn_id = request.turn_id
        yield AnswerDelta("Подбираю ")
        self.first_sent.set()
        await anyio.sleep_forever()
        yield AnswerDelta("не дойдёт")


async def test_client_disconnect_saves_partial_answer_as_interrupted(
    db_session: AsyncSession, shop: TenantId, conversation_id: UUID, db_client: AsyncClient
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
    )
    received: list[bytes] = []

    async def consume() -> None:
        async for chunk in sse_stream(turn):
            received.append(chunk)

    # Так Starlette обрывает стрим при отключении клиента: отменой области anyio.
    async with anyio.create_task_group() as tg:
        tg.start_soon(consume)
        await agent.first_sent.wait()
        tg.cancel_scope.cancel()

    assert len(received) == 2  # turn_started, text_delta
    assert turn.turn_id not in registry
    _, assistant = (await _history(db_client, conversation_id)).messages
    assert (assistant.status, assistant.message_id) == ("interrupted", turn.message_id)
    assert [b.root.model_dump()["text"] for b in assistant.blocks] == ["Подбираю "]


# --- POST /v1/conversations/{id}/turns/{turn_id}/cancel ---


# Сломанная отмена оставила бы GatedAgent ждать вечно: тест должен упасть, а не зависнуть.
WAIT = 5.0


async def _cancel(
    client: AsyncClient, conversation_id: UUID, turn_id: Any, slug: str = "shop"
) -> Response:
    return await client.post(f"{URL}/{conversation_id}/turns/{turn_id}/cancel", headers=_key(slug))


async def test_cancel_interrupts_running_turn(
    app: FastAPI, db_client: AsyncClient, conversation_id: UUID, use_agent: Any
) -> None:
    agent = GatedAgent()
    use_agent(agent)
    stream = asyncio.create_task(_send(db_client, conversation_id))
    await asyncio.wait_for(agent.first_sent.wait(), WAIT)

    cancel = await _cancel(db_client, conversation_id, agent.turn_id)
    events = [e.model_dump() for _, e in _parse_sse((await asyncio.wait_for(stream, WAIT)).text)]

    assert cancel.status_code == 204 and cancel.content == b""
    assert [e["type"] for e in events] == ["turn_started", "text_delta", "text_done", "done"]
    assert events[-1]["data"]["status"] == "interrupted"
    assert {e["turn_id"] for e in events} == {str(agent.turn_id)}
    _, assistant = (await _history(db_client, conversation_id)).messages
    assert assistant.status == "interrupted"
    assert [b.root.model_dump()["text"] for b in assistant.blocks] == ["Подбираю "]
    assert agent.turn_id not in app.state.turn_registry


async def test_cancel_of_foreign_unknown_or_finished_turn_is_404(
    db_client: AsyncClient, db_session: AsyncSession, conversation_id: UUID, use_agent: Any
) -> None:
    await _tenant(db_session, "other")
    created = await db_client.post(URL, json={"visitor_id": "v-2"}, headers=_key("shop"))
    other_conversation = UUID(created.json()["conversation_id"])
    agent = GatedAgent()
    use_agent(agent)
    stream = asyncio.create_task(_send(db_client, conversation_id))
    await asyncio.wait_for(agent.first_sent.wait(), WAIT)

    rejected = [
        await _cancel(db_client, conversation_id, agent.turn_id, slug="other"),
        await _cancel(db_client, other_conversation, agent.turn_id),
        await _cancel(db_client, conversation_id, uuid4()),
    ]
    owner = await _cancel(db_client, conversation_id, agent.turn_id)
    await asyncio.wait_for(stream, WAIT)
    finished = await _cancel(db_client, conversation_id, agent.turn_id)

    for response in [*rejected, finished]:
        assert response.status_code == 404
        assert _error_code(response) == "not_found"
    assert owner.status_code == 204


async def test_cancel_without_key_is_401(db_client: AsyncClient, conversation_id: UUID) -> None:
    response = await db_client.post(f"{URL}/{conversation_id}/turns/{uuid4()}/cancel")

    assert response.status_code == 401
    assert _error_code(response) == "unauthorized"


async def test_cancel_with_malformed_turn_id_is_422(
    db_client: AsyncClient, conversation_id: UUID
) -> None:
    response = await _cancel(db_client, conversation_id, "not-a-uuid")

    assert response.status_code == 422
    assert _error_code(response) == "invalid_input"


class RecordingAgent:
    def __init__(self) -> None:
        self.calls = 0

    async def run_turn(self, request: TurnRequest) -> AsyncIterator[AgentEvent]:
        self.calls += 1
        yield AnswerDelta("ответ")


async def test_cancel_before_agent_start_skips_agent(
    db_session: AsyncSession, shop: TenantId, conversation_id: UUID
) -> None:
    agent, registry = RecordingAgent(), TurnRegistry()
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
    )
    events = turn.events()
    first = await anext(events)

    assert registry.cancel(shop, conversation_id, turn.turn_id)
    rest = [e.model_dump() async for e in events]

    assert first.root.type == "turn_started"
    assert [(e["type"], e["data"]) for e in rest] == [
        ("done", {"status": "interrupted", "usage": None})
    ]
    assert agent.calls == 0


# --- Маппинг событий агента и ход через агентный цикл ---

CARD = {"type": "info_card", "title": "Доставка", "body_markdown": "1-3 дня"}


async def test_tool_and_component_split_text_into_blocks(
    db_client: AsyncClient,
    db_session: AsyncSession,
    shop: TenantId,
    conversation_id: UUID,
    use_agent: Any,
) -> None:
    use_agent(
        ScriptedAgent(
            [
                AnswerDelta("Смотрю. "),
                ToolStarted("call_1", "get_delivery"),
                ToolFinished(
                    "call_1",
                    "get_delivery",
                    ok=True,
                    arguments={"city": "Москва"},
                    content={"days": "1-3"},
                    duration_ms=17,
                ),
                ComponentEmitted(CARD),
                AnswerDelta("Что-то ещё?"),
                TurnCompleted(FinishReason.ANSWERED, Usage(30, 8), steps=2),
            ]
        )
    )

    events = [e.model_dump() for _, e in _parse_sse((await _send(db_client, conversation_id)).text)]

    assert [(e["type"], e["data"].get("block_id")) for e in events] == [
        ("turn_started", None),
        ("text_delta", "b1"),
        ("text_done", "b1"),
        ("tool_started", None),
        ("tool_finished", None),
        ("component", "b2"),
        ("text_delta", "b3"),
        ("text_done", "b3"),
        ("done", None),
    ]
    assert events[3]["data"] == {
        "tool_call_id": "call_1",
        "name": "get_delivery",
        "display_label": None,
    }
    # Аргументы и результат инструмента в клиент не уходят.
    assert events[4]["data"] == {"tool_call_id": "call_1", "ok": True, "duration_ms": 17}
    assert events[-1]["data"]["usage"] == {"input_tokens": 30, "output_tokens": 8, "tool_calls": 1}
    _, assistant = (await _history(db_client, conversation_id)).messages
    assert [b.root.model_dump(exclude_none=True) for b in assistant.blocks] == [
        {"type": "text", "block_id": "b1", "text": "Смотрю. "},
        {"type": "component", "block_id": "b2", "component": CARD},
        {"type": "text", "block_id": "b3", "text": "Что-то ещё?"},
    ]
    assert await ToolCallRepository(db_session).list_for(shop, assistant.message_id) == [
        ToolCallEntry(
            tool_call_id="call_1",
            name="get_delivery",
            arguments={"city": "Москва"},
            result={"days": "1-3"},
            error=None,
            duration_ms=17,
        )
    ]


async def test_failed_tool_call_is_saved_with_error(
    db_client: AsyncClient,
    db_session: AsyncSession,
    shop: TenantId,
    conversation_id: UUID,
    use_agent: Any,
) -> None:
    use_agent(
        ScriptedAgent(
            [
                ToolStarted("call_1", "search_catalog"),
                ToolFinished(
                    "call_1",
                    "search_catalog",
                    ok=False,
                    error_code="validation_error",
                    arguments="{query:",
                    error_message="аргументы должны быть JSON-объектом",
                ),
                AnswerDelta("Уточните запрос."),
                TurnCompleted(FinishReason.ANSWERED, Usage(), steps=2),
            ]
        )
    )

    await _send(db_client, conversation_id)

    _, assistant = (await _history(db_client, conversation_id)).messages
    [saved] = await ToolCallRepository(db_session).list_for(shop, assistant.message_id)
    assert (saved.arguments, saved.result, saved.error) == (
        "{query:",
        None,
        {"code": "validation_error", "message": "аргументы должны быть JSON-объектом"},
    )


class ToolThenGateAgent(GatedAgent):
    """Завершает вызов инструмента, начинает второй и ждёт отмены."""

    async def run_turn(self, request: TurnRequest) -> AsyncIterator[AgentEvent]:
        self.turn_id = request.turn_id
        yield ToolStarted("call_1", "search_catalog")
        yield ToolFinished("call_1", "search_catalog", ok=True, arguments={}, content="ok")
        yield ToolStarted("call_2", "get_entity")
        self.first_sent.set()
        await anyio.sleep_forever()
        yield ToolFinished("call_2", "get_entity", ok=True)


async def test_cancelled_turn_keeps_only_finished_tool_calls(
    db_client: AsyncClient,
    db_session: AsyncSession,
    shop: TenantId,
    conversation_id: UUID,
    use_agent: Any,
) -> None:
    agent = ToolThenGateAgent()
    use_agent(agent)
    stream = asyncio.create_task(_send(db_client, conversation_id))
    await asyncio.wait_for(agent.first_sent.wait(), WAIT)

    await _cancel(db_client, conversation_id, agent.turn_id)
    await asyncio.wait_for(stream, WAIT)

    _, assistant = (await _history(db_client, conversation_id)).messages
    assert assistant.status == "interrupted"
    saved = await ToolCallRepository(db_session).list_for(shop, assistant.message_id)
    assert [c.tool_call_id for c in saved] == ["call_1"]


@pytest.mark.parametrize("retryable", [True, False])
async def test_llm_error_ends_turn_with_llm_unavailable(
    db_client: AsyncClient, conversation_id: UUID, use_agent: Any, retryable: bool
) -> None:
    use_agent(FailingAgent(LLMError("503 от провайдера", retryable=retryable)))

    events = [e.model_dump() for _, e in _parse_sse((await _send(db_client, conversation_id)).text)]

    assert [e["type"] for e in events] == ["turn_started", "text_delta", "error", "done"]
    assert (events[2]["data"]["code"], events[2]["data"]["retryable"]) == (
        "llm_unavailable",
        retryable,
    )
    assert events[3]["data"]["status"] == "failed"


async def test_dialog_through_agent_loop_keeps_history(
    db_client: AsyncClient, conversation_id: UUID, use_agent: Any
) -> None:
    llm = FakeLLM(
        [
            FakeReply(text=ANSWER, usage=Usage(40, 6)),
            FakeReply(text="Тогда посмотрите наборы до 3000.", usage=Usage(55, 9)),
        ]
    )
    providers: list[str] = []

    def llm_for(provider: str) -> FakeLLM:
        providers.append(provider)
        return llm

    use_agent(LoopTurnAgent(llm_for, lambda _: RegistryToolExecutor(ToolRegistry([]))))

    first = _parse_sse((await _send(db_client, conversation_id)).text)
    await _send(db_client, conversation_id, {"type": "text", "text": "До 3000"})

    assert first[-1][1].model_dump()["data"]["usage"] == {
        "input_tokens": 40,
        "output_tokens": 6,
        "tool_calls": 0,
    }
    assert providers == ["openai", "openai"]
    second = llm.requests[1]
    assert second.model == "test-model"
    assert second.tools == () and second.temperature is None
    assert "Ты — консультант shop." in second.system
    assert [type(m).__name__ for m in second.messages] == [
        "UserMessage",
        "AssistantMessage",
        "UserMessage",
    ]
    assert second.messages[1].text == ANSWER  # type: ignore[union-attr]
    texts = [
        m.blocks[0].root.model_dump()["text"]
        for m in (await _history(db_client, conversation_id)).messages
        if m.role == "assistant"
    ]
    assert texts == [ANSWER, "Тогда посмотрите наборы до 3000."]


# --- Действия и быстрые ответы (P5-03a) ---


async def test_action_with_label_and_suggestions_through_agent_loop(
    db_client: AsyncClient, conversation_id: UUID, use_agent: Any
) -> None:
    suggest = ToolCall(
        id="call_1",
        name="suggest_replies",
        arguments={"options": ["Сухая", "Жирная"]},
        raw_arguments='{"options": ["Сухая", "Жирная"]}',
    )
    llm = FakeLLM([FakeReply(text="Какой у вас тип кожи?", tool_calls=(suggest,)), FakeReply()])
    use_agent(
        LoopTurnAgent(
            lambda _: llm, lambda _: RegistryToolExecutor(ToolRegistry([suggest_replies_tool()]))
        )
    )
    action = {
        "type": "action",
        "action_id": "ask_about",
        "label": "Подробнее — Крем",
        "payload": {"entity_id": "e1"},
    }

    events = [
        e.model_dump()
        for _, e in _parse_sse((await _send(db_client, conversation_id, action)).text)
    ]

    [suggestions] = [e["data"] for e in events if e["type"] == "suggestions"]
    assert suggestions == {
        "items": [
            {"label": "Сухая", "input": {"type": "text", "text": "Сухая"}},
            {"label": "Жирная", "input": {"type": "text", "text": "Жирная"}},
        ]
    }
    assert events[-1]["type"] == "done" and events[-1]["data"]["status"] == "completed"
    # Модель видит подпись кнопки, история хранит исходный ввод с подписью.
    assert llm.requests[0].messages[-1] == UserMessage(
        '[Нажата кнопка «Подробнее — Крем» (ask_about) {"entity_id": "e1"}]'
    )
    user, _ = (await _history(db_client, conversation_id)).messages
    assert user.input is not None
    assert user.input.root.model_dump() == action


# --- DialogState (P2-09) ---


async def _state(session: AsyncSession, tenant_id: TenantId, conversation_id: UUID) -> Any:
    conversation = await ConversationRepository(session).find(tenant_id, conversation_id)
    assert conversation is not None
    return conversation.state


async def test_agent_does_not_reask_known_slot(
    db_client: AsyncClient, db_session: AsyncSession, use_agent: Any
) -> None:
    """DoD: слот, записанный на первом ходе, на втором ходе — в Runtime-слое промпта."""
    config = _config("beauty")
    config["prompt"]["scenarios"] = [
        {
            "key": "skincare",
            "description": "Подбор ухода",
            "instructions": "Выясни бюджет и тип кожи.",
            "slots": {"budget": {"type": "number"}, "skin_type": {"type": "string"}},
        }
    ]
    config["tools"] = {"builtin": ["update_dialog_state", "search_catalog"]}
    beauty = await _tenant(db_session, "beauty", config)
    created = await db_client.post(URL, json={"visitor_id": "v-1"}, headers=_key("beauty"))
    conversation_id = UUID(created.json()["conversation_id"])
    remember = ToolCall(
        id="call_1",
        name="update_dialog_state",
        arguments={"slots": {"budget": 3000}},
        raw_arguments='{"slots": {"budget": 3000}}',
    )
    llm = FakeLLM(
        [
            FakeReply(tool_calls=(remember,)),
            FakeReply(text="Какой у вас тип кожи?"),
            FakeReply(text="Подберу крем для сухой кожи до 3000."),
        ]
    )
    use_agent(
        LoopTurnAgent(
            lambda _: llm,
            lambda c: RegistryToolExecutor(ToolRegistry(builtin_tools(c))),
        )
    )

    await _send(
        db_client, conversation_id, {"type": "text", "text": "Крем, бюджет 3000"}, slug="beauty"
    )
    await _send(db_client, conversation_id, {"type": "text", "text": "Сухая"}, slug="beauty")

    first, _, second = llm.requests
    assert [t.name for t in first.tools] == ["update_dialog_state"]
    assert "пока ничего не известно" in first.system
    runtime = second.system.split("<runtime>")[1]
    known = runtime.split("не переспрашивай):")[1]
    assert '"budget": 3000' in known
    assert await _state(db_session, beauty, conversation_id) == {"slots": {"budget": 3000}}


class StateThenGateAgent(GatedAgent):
    """Обновляет состояние и ждёт отмены."""

    async def run_turn(self, request: TurnRequest) -> AsyncIterator[AgentEvent]:
        self.turn_id = request.turn_id
        yield DialogStateUpdated(DialogState(slots={"budget": 3000}, facts=("спешит",)))
        self.first_sent.set()
        await anyio.sleep_forever()
        yield AnswerDelta("не дойдёт")


async def test_state_is_saved_when_turn_is_cancelled(
    db_client: AsyncClient,
    db_session: AsyncSession,
    shop: TenantId,
    conversation_id: UUID,
    use_agent: Any,
) -> None:
    agent = StateThenGateAgent()
    use_agent(agent)
    stream = asyncio.create_task(_send(db_client, conversation_id))
    await asyncio.wait_for(agent.first_sent.wait(), WAIT)

    await _cancel(db_client, conversation_id, agent.turn_id)
    events = _parse_sse((await asyncio.wait_for(stream, WAIT)).text)

    assert events[-1][1].model_dump()["data"]["status"] == "interrupted"
    # Состояние в клиент не уходит.
    assert all("budget" not in e.model_dump_json() for _, e in events)
    assert await _state(db_session, shop, conversation_id) == {
        "slots": {"budget": 3000},
        "facts": ["спешит"],
    }


async def test_state_is_passed_to_agent_and_unchanged_without_updates(
    db_client: AsyncClient,
    db_session: AsyncSession,
    shop: TenantId,
    conversation_id: UUID,
    use_agent: Any,
) -> None:
    await ConversationRepository(db_session).update_state(
        shop, conversation_id, {"slots": {"budget": 3000}}
    )
    agent = ScriptedAgent()
    use_agent(agent)

    await _send(db_client, conversation_id)

    assert agent.requests[0].dialog_state == {"slots": {"budget": 3000}}
    assert await _state(db_session, shop, conversation_id) == {"slots": {"budget": 3000}}


# --- Формы и подтверждение заявки (P5-04a, ADR-0021) ---

FORMS = {
    "contact": {
        "title": "Контакт",
        "fields": [
            {"name": "phone", "label": "Телефон", "kind": "text", "required": True},
            {
                "name": "time",
                "label": "Когда",
                "kind": "select",
                "options": [{"label": "Утром", "value": "morning"}],
            },
        ],
    }
}


@pytest.fixture
async def forms_tenant(db_session: AsyncSession) -> TenantId:
    return await _tenant(
        db_session,
        "forms",
        {**_config("forms"), "tools": {"builtin": ["create_lead"]}, "forms": FORMS},
    )


@pytest.fixture
async def form_conversation(db_client: AsyncClient, forms_tenant: TenantId, use_agent: Any) -> UUID:
    response = await db_client.post(URL, json={"visitor_id": "v-1"}, headers=_key("forms"))
    return UUID(response.json()["conversation_id"])


@pytest.mark.parametrize(
    ("form_id", "values"),
    [
        ("order", {"phone": "+7900"}),
        ("contact", {}),
        ("contact", {"phone": "+7900", "email": "a@b.c"}),
        ("contact", {"phone": "+7900", "time": "night"}),
    ],
    ids=["unknown_form", "missing_required", "unknown_field", "not_an_option"],
)
async def test_invalid_form_submit_is_422_and_not_saved(
    db_client: AsyncClient, form_conversation: UUID, form_id: str, values: dict[str, Any]
) -> None:
    submit = {"type": "form_submit", "form_id": form_id, "values": values}

    response = await _send(db_client, form_conversation, submit, slug="forms")

    assert response.status_code == 422
    assert _error_code(response) == "invalid_input"
    history = await db_client.get(f"{URL}/{form_conversation}/messages", headers=_key("forms"))
    assert history.json()["messages"] == []


class SessionLeads:
    """Заявки в сессии теста (откатывается после теста), а не через свою сессию с commit."""

    def __init__(self, session: AsyncSession) -> None:
        self._repo = LeadRepository(session)

    async def create(
        self, tenant_id: TenantId, conversation_id: UUID, form_key: str, fields: Mapping[str, str]
    ) -> UUID:
        return (await self._repo.create(tenant_id, conversation_id, form_key, fields)).id


async def _events(response: Response) -> list[dict[str, Any]]:
    return [e.model_dump() for _, e in _parse_sse(response.text)]


async def test_form_submit_then_confirmed_lead_through_agent_loop(
    db_session: AsyncSession,
    db_client: AsyncClient,
    forms_tenant: TenantId,
    form_conversation: UUID,
    use_agent: Any,
) -> None:
    create = ToolCall(
        id="call_1",
        name="create_lead",
        arguments={"form_key": "contact", "fields": {"phone": "+7900"}},
        raw_arguments="{}",
    )
    llm = FakeLLM(
        [
            FakeReply(text="Проверьте заявку.", tool_calls=(create,)),
            FakeReply(),
            FakeReply(text="Заявка отправлена."),
        ]
    )
    use_agent(builtin_turn_agent(lambda _: llm, leads=SessionLeads(db_session)))
    submit = {"type": "form_submit", "form_id": "contact", "values": {"phone": "+7900"}}

    first = await _events(await _send(db_client, form_conversation, submit, slug="forms"))
    [confirm] = [e["data"]["component"] for e in first if e["type"] == "component"]
    leads = LeadRepository(db_session)
    assert confirm["type"] == "confirm"
    assert await leads.list_for_conversation(forms_tenant, form_conversation) == []
    # Модель видит отправленную форму.
    assert llm.requests[0].messages[-1] == UserMessage(
        '[Отправлена форма contact: {"phone": "+7900"}]'
    )

    action = confirm["confirm_action"]
    press = {
        "type": "action",
        "action_id": action["action_id"],
        "label": action["label"],
        "payload": action["payload"],
    }
    second = await _events(await _send(db_client, form_conversation, press, slug="forms"))

    [lead] = await leads.list_for_conversation(forms_tenant, form_conversation)
    assert (lead.form_key, lead.fields) == ("contact", {"phone": "+7900"})
    assert [e["type"] for e in second][:3] == ["turn_started", "tool_started", "tool_finished"]
    assert second[-1]["data"]["status"] == "completed"
    assert "pending_confirmation" not in await _state(db_session, forms_tenant, form_conversation)
