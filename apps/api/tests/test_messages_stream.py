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
from starlette.requests import ClientDisconnect

from app.contracts import Event, HttpError, MessageHistory, UserInput
from app.logs import TRACE_ID_HEADER
from app.modules.agent.application import fallback_llm
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
    TurnTimeoutError,
    Usage,
    UserMessage,
)
from app.modules.chat.api.router import get_turn_agent
from app.modules.chat.api.sse import TurnResponse, sse_stream
from app.modules.chat.application.turns import TurnRegistry, retry_turn, start_turn
from app.modules.chat.domain.entities import (
    ChatMessage,
    Conversation,
    MessageRole,
    NewMessage,
    ToolCallEntry,
    TurnRequest,
)
from app.modules.chat.domain.ports import TurnAgent
from app.modules.chat.infrastructure.loop_agent import LoopTurnAgent, builtin_turn_agent
from app.modules.chat.infrastructure.models import MessageRecord
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
    AgentConfigRecord,
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


# --- Запись ответа: сбой и обрыв во время записи (REVIEW.md: C-1) ---


class FlakyMessages(MessageRepository):
    """Запись ответа ассистента падает `failures` раз (таймаут пула) или ждёт `gate`."""

    def __init__(
        self, session: AsyncSession, failures: int = 0, gate: anyio.Event | None = None
    ) -> None:
        super().__init__(session)
        self.failures = failures
        self.gate = gate
        self.saving = anyio.Event()

    async def add_once(self, tenant_id: TenantId, message: NewMessage) -> tuple[ChatMessage, bool]:
        if message.role is MessageRole.ASSISTANT:
            self.saving.set()
            if self.gate is not None:
                await self.gate.wait()
            if self.failures > 0:
                self.failures -= 1
                raise TimeoutError("QueuePool limit reached, connection timed out")
        return await super().add_once(tenant_id, message)


async def _flaky_turn(
    db_session: AsyncSession, shop: TenantId, conversation_id: UUID, messages: FlakyMessages
) -> Any:
    return await start_turn(
        shop,
        conversation_id,
        uuid4(),
        UserInput.model_validate(TEXT),
        ConversationRepository(db_session),
        messages,
        ToolCallRepository(db_session),
        TenantsAgentConfigs(db_session),
        ScriptedAgent(),
        db_session.commit,
        TurnRegistry(),
        rollback=db_session.rollback,
    )


async def test_failed_save_ends_stream_with_error_and_keeps_answer_as_failed(
    db_client: AsyncClient, db_session: AsyncSession, shop: TenantId, conversation_id: UUID
) -> None:
    turn = await _flaky_turn(
        db_session, shop, conversation_id, FlakyMessages(db_session, failures=1)
    )

    events = [e.root async for e in turn.events()]

    assert [e.type for e in events[-2:]] == ["error", "done"]
    assert events[-2].data.model_dump()["code"] == "internal"
    assert events[-1].data.model_dump()["status"] == "failed"
    _, assistant = (await _history(db_client, conversation_id)).messages
    assert (assistant.message_id, assistant.status) == (turn.message_id, "failed")
    assert assistant.error is not None and assistant.error.code == "internal"
    assert "".join(b.root.model_dump()["text"] for b in assistant.blocks) == ANSWER


async def test_stream_ends_with_done_even_if_answer_cannot_be_saved(
    db_client: AsyncClient, db_session: AsyncSession, shop: TenantId, conversation_id: UUID
) -> None:
    turn = await _flaky_turn(
        db_session, shop, conversation_id, FlakyMessages(db_session, failures=2)
    )

    types = [e.root.type async for e in turn.events()]

    assert types[-2:] == ["error", "done"]
    assert len((await _history(db_client, conversation_id)).messages) == 1
    # При закрытии стрима — ещё одна попытка: ответ не потерян.
    await turn.close()
    _, assistant = (await _history(db_client, conversation_id)).messages
    assert assistant.status == "interrupted"


async def test_client_disconnect_during_save_does_not_lose_answer(
    db_client: AsyncClient, db_session: AsyncSession, shop: TenantId, conversation_id: UUID
) -> None:
    gate = anyio.Event()
    messages = FlakyMessages(db_session, gate=gate)
    turn = await _flaky_turn(db_session, shop, conversation_id, messages)

    async def consume() -> None:
        async for _ in sse_stream(turn):
            pass

    # Клиент закрыл виджет, пока ответ записывается: Starlette отменяет стрим.
    async with anyio.create_task_group() as tg:
        tg.start_soon(consume)
        with anyio.fail_after(WAIT):
            await messages.saving.wait()
        tg.cancel_scope.cancel()
        gate.set()

    _, assistant = (await _history(db_client, conversation_id)).messages
    assert (assistant.message_id, assistant.status) == (turn.message_id, "completed")
    assert "".join(b.root.model_dump()["text"] for b in assistant.blocks) == ANSWER


class SlowAgent:
    """Долгий инструмент: между событиями агент молчит дольше интервала пинга."""

    async def run_turn(self, request: TurnRequest) -> AsyncIterator[AgentEvent]:
        yield AnswerDelta("Ищу ")
        await anyio.sleep(0.2)
        yield AnswerDelta("нашла.")
        yield TurnCompleted(FinishReason.ANSWERED, Usage(1, 1), steps=1)


async def test_silent_agent_gets_keep_alive_pings(
    db_session: AsyncSession, shop: TenantId, conversation_id: UUID
) -> None:
    turn = await start_turn(
        shop,
        conversation_id,
        uuid4(),
        UserInput.model_validate(TEXT),
        ConversationRepository(db_session),
        MessageRepository(db_session),
        ToolCallRepository(db_session),
        TenantsAgentConfigs(db_session),
        SlowAgent(),
        db_session.commit,
        TurnRegistry(),
    )

    chunks = [chunk async for chunk in sse_stream(turn, ping_after_s=0.05)]

    # Прокси не оборвёт стрим по простою (REVIEW.md: C-6); клиенты EventSource пинг игнорируют.
    events = [c.split(b"\n", 1)[0] for c in chunks if c != b": ping\n\n"]
    assert b": ping\n\n" in chunks
    assert events[0] == b"event: turn_started"
    assert events[-1] == b"event: done"
    first_ping = chunks.index(b": ping\n\n")
    assert chunks[first_ping - 1].startswith(b"event: text_delta")


async def test_turn_stream_disables_caching_and_proxy_buffering(
    db_client: AsyncClient, conversation_id: UUID
) -> None:
    response = await _send(db_client, conversation_id)

    assert response.headers["content-type"].startswith("text/event-stream")
    assert response.headers["cache-control"] == "no-cache"
    assert response.headers["x-accel-buffering"] == "no"


@pytest.mark.parametrize("retry", [False, True], ids=["send", "retry"])
async def test_turn_holds_no_transaction_while_agent_answers(
    db_session: AsyncSession,
    shop: TenantId,
    conversation_id: UUID,
    db_client: AsyncClient,
    use_agent: Any,
    retry: bool,
) -> None:
    """ADR-0034: пока модель отвечает, соединение пула свободно — транзакция подготовки
    хода закрыта до стрима, ответ пишется своей короткой транзакцией."""
    stores = (
        ConversationRepository(db_session),
        MessageRepository(db_session),
        ToolCallRepository(db_session),
        TenantsAgentConfigs(db_session),
    )
    agent, registry = GatedAgent(), TurnRegistry()
    if retry:
        use_agent(FailingAgent(FLAKY))
        failed_id = _parse_sse((await _send(db_client, conversation_id)).text)[-1][1].root
        assert failed_id.message_id is not None
        turn = await retry_turn(
            shop,
            conversation_id,
            UUID(failed_id.message_id),
            *stores,
            agent,
            db_session.commit,
            registry,
        )
    else:
        turn = await start_turn(
            shop,
            conversation_id,
            uuid4(),
            UserInput.model_validate(TEXT),
            *stores,
            agent,
            db_session.commit,
            registry,
        )
    assert not db_session.in_transaction()
    events = turn.events()
    await anext(events)  # turn_started
    await anext(events)  # text_delta: агент ждёт внутри ответа
    assert not db_session.in_transaction()

    assert await registry.cancel(shop, conversation_id, turn.turn_id)
    rest = [e.root.type async for e in events]

    assert rest == ["text_done", "done"]
    assert not db_session.in_transaction()
    assistant = (await _history(db_client, conversation_id)).messages[-1]
    assert (assistant.message_id, assistant.status) == (turn.message_id, "interrupted")


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


# --- Один ход на диалог (ADR-0041) ---


@pytest.mark.parametrize("retry", [False, True], ids=["send", "retry"])
async def test_second_turn_during_turn_is_409_and_not_saved(
    db_client: AsyncClient, conversation_id: UUID, use_agent: Any, retry: bool
) -> None:
    use_agent(FailingAgent(FLAKY))
    failed = _parse_sse((await _send(db_client, conversation_id)).text)[-1][1].root
    agent = GatedAgent()
    use_agent(agent)
    stream = asyncio.create_task(_send(db_client, conversation_id))
    await asyncio.wait_for(agent.first_sent.wait(), WAIT)
    before = (await _history(db_client, conversation_id)).messages

    second = await (
        _retry(db_client, conversation_id, str(failed.message_id))
        if retry
        else _send(db_client, conversation_id)
    )

    assert second.status_code == 409
    assert HttpError.model_validate(second.json()).error.model_dump(
        include={"code", "retryable"}
    ) == {
        "code": "turn_in_progress",
        "retryable": True,
    }
    assert (await _history(db_client, conversation_id)).messages == before
    assert (await _cancel(db_client, conversation_id, agent.turn_id)).status_code == 204
    await asyncio.wait_for(stream, WAIT)
    use_agent(ScriptedAgent())
    assert (await _send(db_client, conversation_id)).status_code == 200


async def test_foreign_tenant_on_busy_conversation_is_404_and_does_not_block_it(
    db_client: AsyncClient, db_session: AsyncSession, conversation_id: UUID, use_agent: Any
) -> None:
    """Занятость диалога — в пределах тенанта: чужой ключ виджета не узнаёт, что в диалоге
    идёт ход, и не занимает его."""
    await _tenant(db_session, "other")
    agent = GatedAgent()
    use_agent(agent)
    stream = asyncio.create_task(_send(db_client, conversation_id))
    await asyncio.wait_for(agent.first_sent.wait(), WAIT)

    foreign = await _send(db_client, conversation_id, slug="other")

    assert (foreign.status_code, _error_code(foreign)) == (404, "conversation_not_found")
    assert _error_code(await _send(db_client, conversation_id)) == "turn_in_progress"
    assert (await _cancel(db_client, conversation_id, agent.turn_id)).status_code == 204
    await asyncio.wait_for(stream, WAIT)


async def test_rejected_send_frees_conversation(
    db_client: AsyncClient, conversation_id: UUID
) -> None:
    client_message_id = uuid4()
    assert (await _send(db_client, conversation_id, client_message_id=client_message_id)).is_success

    repeated = await _send(db_client, conversation_id, client_message_id=client_message_id)

    assert _error_code(repeated) == "duplicate_message"
    assert (await _send(db_client, conversation_id)).is_success


async def test_response_closed_before_stream_frees_conversation(
    shop: TenantId, conversation_id: UUID, db_session: AsyncSession, db_client: AsyncClient
) -> None:
    """Клиент отключился до первого события: Starlette не запускает генератор стрима,
    ход закрывает сам ответ — диалог свободен, ответ записан как прерванный."""
    registry = TurnRegistry()
    turn = await start_turn(
        shop,
        conversation_id,
        uuid4(),
        UserInput.model_validate(TEXT),
        ConversationRepository(db_session),
        MessageRepository(db_session),
        ToolCallRepository(db_session),
        TenantsAgentConfigs(db_session),
        ScriptedAgent(),
        db_session.commit,
        registry,
    )

    async def receive() -> dict[str, Any]:
        await anyio.sleep_forever()
        return {}

    async def send(message: Mapping[str, Any]) -> None:
        raise OSError("клиент отключился")

    with pytest.raises(ClientDisconnect):
        await TurnResponse(turn)({"type": "http", "asgi": {"spec_version": "2.4"}}, receive, send)

    assert await registry.acquire(shop, conversation_id, uuid4())
    _, assistant = (await _history(db_client, conversation_id)).messages
    assert assistant.status == "interrupted"


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

    assert await registry.cancel(shop, conversation_id, turn.turn_id)
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
                ToolStarted("call_1", "get_delivery", "Уточняю доставку"),
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
        "display_label": "Уточняю доставку",
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


async def test_turn_timeout_ends_turn_with_retryable_error(
    db_client: AsyncClient, conversation_id: UUID, use_agent: Any
) -> None:
    use_agent(FailingAgent(TurnTimeoutError("ход не уложился в 60 с")))

    events = [e.model_dump() for _, e in _parse_sse((await _send(db_client, conversation_id)).text)]

    assert [e["type"] for e in events] == ["turn_started", "text_delta", "error", "done"]
    assert (events[2]["data"]["code"], events[2]["data"]["retryable"]) == ("turn_timeout", True)
    assert events[3]["data"]["status"] == "failed"
    _, assistant = (await _history(db_client, conversation_id)).messages
    # Показанный до таймаута текст сохранён вместе с ошибкой.
    assert assistant.status == "failed"
    assert [b.model_dump()["text"] for b in assistant.blocks] == ["Сейчас "]
    assert assistant.error is not None and assistant.error.code == "turn_timeout"


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

    use_agent(LoopTurnAgent(llm_for, lambda *_: RegistryToolExecutor(ToolRegistry([]))))

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
            lambda _: llm, lambda *_: RegistryToolExecutor(ToolRegistry([suggest_replies_tool()]))
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
        arguments={"scenario": "skincare", "slots": {"budget": 3000}},
        raw_arguments='{"scenario": "skincare", "slots": {"budget": 3000}}',
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
            lambda c, _: RegistryToolExecutor(ToolRegistry(builtin_tools(c))),
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
    assert await _state(db_session, beauty, conversation_id) == {
        "slots": {"skincare": {"budget": 3000}},
        "active_scenario": "skincare",
    }


class StateThenGateAgent(GatedAgent):
    """Обновляет состояние и ждёт отмены."""

    async def run_turn(self, request: TurnRequest) -> AsyncIterator[AgentEvent]:
        self.turn_id = request.turn_id
        yield DialogStateUpdated(
            DialogState(slots={"skincare": {"budget": 3000}}, facts=("спешит",))
        )
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
        "slots": {"skincare": {"budget": 3000}},
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
        ("contact", {"phone": "7" * 201}),
        ("contact", {"phone": "+7900\u0000"}),
    ],
    ids=[
        "unknown_form",
        "missing_required",
        "unknown_field",
        "not_an_option",
        "too_long",
        "control_char",
    ],
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
        self,
        tenant_id: TenantId,
        conversation_id: UUID,
        form_key: str,
        fields: Mapping[str, str],
        *,
        idempotency_key: str,
    ) -> UUID:
        lead = await self._repo.create(
            tenant_id, conversation_id, form_key, fields, idempotency_key=idempotency_key
        )
        return lead.id


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


class FrozenConversations(ConversationRepository):
    """Диалог читается таким, каким был до снятия ожидания другим ходом: гонка двух ходов,
    прочитавших состояние раньше, чем один из них снял ожидание."""

    def __init__(self, session: AsyncSession, snapshot: Conversation) -> None:
        super().__init__(session)
        self.snapshot = snapshot

    async def find(self, tenant_id: TenantId, conversation_id: UUID) -> Conversation | None:
        return self.snapshot


@pytest.mark.parametrize("snapshot", [False, True], ids=["after_claim", "read_before_claim"])
async def test_confirmation_pressed_twice_creates_one_lead(
    db_session: AsyncSession,
    db_client: AsyncClient,
    forms_tenant: TenantId,
    form_conversation: UUID,
    use_agent: Any,
    snapshot: bool,
) -> None:
    """M-1, ADR-0041: два хода с одним confirm_id (две вкладки, двойной клик, разные
    процессы без Redis) — заявка одна, второй ход отвечает, что подтверждение устарело."""
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
            FakeReply(text="Заявка уже отправлена."),
        ]
    )
    agent = builtin_turn_agent(lambda _: llm, leads=SessionLeads(db_session))
    use_agent(agent)
    submit = {"type": "form_submit", "form_id": "contact", "values": {"phone": "+7900"}}
    first = await _events(await _send(db_client, form_conversation, submit, slug="forms"))
    [confirm] = [e["data"]["component"] for e in first if e["type"] == "component"]
    action = confirm["confirm_action"]
    press = UserInput.model_validate(
        {"type": "action", "action_id": action["action_id"], "payload": action["payload"]}
    )
    before = await ConversationRepository(db_session).find(forms_tenant, form_conversation)
    assert before is not None and "pending_confirmation" in before.state

    def stores(
        conversations: ConversationRepository,
    ) -> tuple[ConversationRepository, MessageRepository, ToolCallRepository, TenantsAgentConfigs]:
        return (
            conversations,
            MessageRepository(db_session),
            ToolCallRepository(db_session),
            TenantsAgentConfigs(db_session),
        )

    # Два хода подготовлены до того, как первый исполнит действие; реестры разные — как
    # процессы API, не видящие друг друга (Redis недоступен).
    turns = [
        await start_turn(
            forms_tenant,
            form_conversation,
            uuid4(),
            press,
            *stores(
                FrozenConversations(db_session, before)
                if snapshot and i
                else ConversationRepository(db_session)
            ),
            agent,
            db_session.commit,
            TurnRegistry(),
        )
        for i in range(2)
    ]
    results = [[e.root.type async for e in turn.events()] for turn in turns]

    leads = await LeadRepository(db_session).list_for_conversation(forms_tenant, form_conversation)
    assert len(leads) == 1
    assert "tool_started" in results[0]
    assert "tool_started" not in results[1] and results[1][-1] == "done"
    assert "устарело" in str(llm.requests[-1].messages[-1])
    assert "pending_confirmation" not in await _state(db_session, forms_tenant, form_conversation)


class StateAgent:
    """Ход без подтверждения, который меняет только факты — со своим (старым) ожиданием
    в состоянии, как его прочитал ход."""

    async def run_turn(self, request: TurnRequest) -> AsyncIterator[AgentEvent]:
        state = DialogState.from_dict(request.dialog_state).apply({"facts": ["спешит"]})
        yield DialogStateUpdated(state)
        yield TurnCompleted(FinishReason.ANSWERED, Usage(1, 1), steps=1)


async def test_turn_does_not_restore_confirmation_claimed_by_other_turn(
    db_session: AsyncSession,
    db_client: AsyncClient,
    forms_tenant: TenantId,
    form_conversation: UUID,
    use_agent: Any,
) -> None:
    """Ход прочитал состояние с ожиданием, другой ход его снял и исполнил: запись
    состояния первым ходом не возвращает ожидание — повторное нажатие не даст вторую заявку."""
    pending = {"confirm_id": "c-1", "tool": "create_lead", "arguments": {}}
    repo = ConversationRepository(db_session)
    await repo.update_state(forms_tenant, form_conversation, {"pending_confirmation": pending})
    await db_session.commit()
    snapshot = await repo.find(forms_tenant, form_conversation)
    assert snapshot is not None
    turn = await start_turn(
        forms_tenant,
        form_conversation,
        uuid4(),
        UserInput.model_validate(TEXT),
        FrozenConversations(db_session, snapshot),
        MessageRepository(db_session),
        ToolCallRepository(db_session),
        TenantsAgentConfigs(db_session),
        StateAgent(),
        db_session.commit,
        TurnRegistry(),
    )
    assert await repo.claim_confirmation(forms_tenant, form_conversation, "c-1")
    await db_session.commit()

    assert [e.root.type async for e in turn.events()][-1] == "done"

    assert await _state(db_session, forms_tenant, form_conversation) == {"facts": ["спешит"]}


async def test_retry_of_confirmation_claims_it_before_execution(
    db_session: AsyncSession,
    db_client: AsyncClient,
    forms_tenant: TenantId,
    form_conversation: UUID,
    use_agent: Any,
) -> None:
    """Повтор нажатия снимает ожидание тем же условным UPDATE: повтор и новое нажатие той же
    кнопки (другой процесс) исполняют действие один раз."""
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
            FakeReply(text="Заявка уже отправлена."),
        ]
    )
    agent = builtin_turn_agent(lambda _: llm, leads=SessionLeads(db_session))
    use_agent(agent)
    submit = {"type": "form_submit", "form_id": "contact", "values": {"phone": "+7900"}}
    first = await _events(await _send(db_client, form_conversation, submit, slug="forms"))
    [confirm] = [e["data"]["component"] for e in first if e["type"] == "component"]
    action = confirm["confirm_action"]
    press = {"type": "action", "action_id": action["action_id"], "payload": action["payload"]}
    state = await _state(db_session, forms_tenant, form_conversation)
    # Нажатие упало до исполнения; ожидание снова в состоянии (диалог до ADR-0041).
    use_agent(FailingAgent(FLAKY))
    failed = (await _events(await _send(db_client, form_conversation, press, slug="forms")))[-1]
    await ConversationRepository(db_session).update_state(forms_tenant, form_conversation, state)
    await db_session.commit()
    stores = (
        ConversationRepository(db_session),
        MessageRepository(db_session),
        ToolCallRepository(db_session),
        TenantsAgentConfigs(db_session),
    )

    retried = await retry_turn(
        forms_tenant,
        form_conversation,
        UUID(failed["message_id"]),
        *stores,
        agent,
        db_session.commit,
        TurnRegistry(),
    )
    pressed = await start_turn(
        forms_tenant,
        form_conversation,
        uuid4(),
        UserInput.model_validate(press),
        *stores,
        agent,
        db_session.commit,
        TurnRegistry(),
    )
    results = [[e.root.type async for e in turn.events()] for turn in (retried, pressed)]

    leads = await LeadRepository(db_session).list_for_conversation(forms_tenant, form_conversation)
    assert len(leads) == 1
    assert "tool_started" in results[0] and "tool_started" not in results[1]


# --- Повтор неудачного ответа (P5-08a, ADR-0023) ---

FLAKY = LLMError("503 от провайдера", retryable=True)


async def _retry(
    client: AsyncClient, conversation_id: UUID, message_id: UUID | str, slug: str = "shop"
) -> Response:
    return await client.post(
        f"{URL}/{conversation_id}/messages/{message_id}/retry", headers=_key(slug)
    )


def _loop_agent(llm: FakeLLM) -> LoopTurnAgent:
    return LoopTurnAgent(lambda _: llm, lambda *_: RegistryToolExecutor(ToolRegistry([])))


async def test_failed_answer_keeps_error_in_history(
    db_client: AsyncClient, conversation_id: UUID, use_agent: Any
) -> None:
    await _send(db_client, conversation_id)
    use_agent(FailingAgent(FLAKY))
    await _send(db_client, conversation_id, {"type": "text", "text": "А до 3000?"})

    response = await db_client.get(f"{URL}/{conversation_id}/messages", headers=_key("shop"))

    _, answer, _, failed = response.json()["messages"]
    assert "error" not in answer  # у успешного ответа поле опускается
    assert failed["error"] == {
        "code": "llm_unavailable",
        "message": "модель сейчас недоступна",
        "retryable": True,
    }


async def test_retry_replaces_failed_answer_through_agent_loop(
    db_client: AsyncClient, db_session: AsyncSession, conversation_id: UUID, use_agent: Any
) -> None:
    llm = FakeLLM(
        [
            FakeReply(text="Сейчас посмотрю", fail_after_chunks=1, error=FLAKY),
            FakeReply(text=ANSWER),
        ]
    )
    use_agent(_loop_agent(llm))
    failed_id = _parse_sse((await _send(db_client, conversation_id)).text)[-1][1].root.message_id
    assert failed_id is not None

    response = await _retry(db_client, conversation_id, failed_id)

    assert response.status_code == 200
    events = [e.root for _, e in _parse_sse(response.text)]
    assert [events[0].type, events[-1].type] == ["turn_started", "done"]
    new_id = events[-1].message_id
    assert new_id not in (None, failed_id)
    # Модель видит вопрос один раз и не видит обрывок неудачного ответа.
    assert llm.requests[1].messages == (UserMessage(TEXT["text"]),)
    user, assistant = (await _history(db_client, conversation_id)).messages
    assert user.input is not None and user.input.model_dump() == TEXT
    assert (str(assistant.message_id), assistant.status, assistant.error) == (
        new_id,
        "completed",
        None,
    )
    # Неудачный ответ остаётся в БД, помеченный заменённым.
    record = await db_session.get(MessageRecord, UUID(failed_id))
    assert record is not None and str(record.replaced_by) == new_id


async def test_retried_answer_can_fail_and_be_retried_again(
    db_client: AsyncClient, conversation_id: UUID, use_agent: Any
) -> None:
    use_agent(FailingAgent(FLAKY))
    first = _parse_sse((await _send(db_client, conversation_id)).text)[-1][1].root.message_id
    second = _parse_sse((await _retry(db_client, conversation_id, str(first))).text)[-1][1]
    use_agent(ScriptedAgent())

    third = await _retry(db_client, conversation_id, str(second.root.message_id))

    assert third.status_code == 200
    roles = [(m.role, m.status) for m in (await _history(db_client, conversation_id)).messages]
    assert roles == [("user", "completed"), ("assistant", "completed")]


@pytest.mark.parametrize("case", ["completed", "not_retryable", "not_last", "user_message"])
async def test_retry_not_allowed_is_409(
    db_client: AsyncClient, conversation_id: UUID, use_agent: Any, case: str
) -> None:
    if case != "completed":
        use_agent(FailingAgent(FLAKY if case != "not_retryable" else None))
    events = _parse_sse((await _send(db_client, conversation_id)).text)
    if case == "not_last":
        use_agent(ScriptedAgent())
        await _send(db_client, conversation_id, {"type": "text", "text": "Ладно, другое"})
    before = (await _history(db_client, conversation_id)).messages
    target = before[0].message_id if case == "user_message" else events[-1][1].root.message_id

    response = await _retry(db_client, conversation_id, str(target))

    assert response.status_code == 409
    assert _error_code(response) == "not_retryable"
    assert (await _history(db_client, conversation_id)).messages == before


async def test_retry_of_replaced_unknown_or_foreign_is_404(
    db_client: AsyncClient, db_session: AsyncSession, conversation_id: UUID, use_agent: Any
) -> None:
    await _tenant(db_session, "other")
    use_agent(FailingAgent(FLAKY))
    failed_id = str(
        _parse_sse((await _send(db_client, conversation_id)).text)[-1][1].root.message_id
    )
    use_agent(ScriptedAgent())
    await _retry(db_client, conversation_id, failed_id)

    replaced = await _retry(db_client, conversation_id, failed_id)
    unknown = await _retry(db_client, conversation_id, uuid4())
    foreign = await _retry(db_client, conversation_id, failed_id, slug="other")
    missing = await _retry(db_client, uuid4(), failed_id)

    assert [r.status_code for r in (replaced, unknown, foreign, missing)] == [404] * 4
    assert [_error_code(r) for r in (replaced, unknown, foreign, missing)] == [
        "not_found",
        "not_found",
        "conversation_not_found",
        "conversation_not_found",
    ]


async def test_retry_without_key_is_401(db_client: AsyncClient, conversation_id: UUID) -> None:
    response = await db_client.post(f"{URL}/{conversation_id}/messages/{uuid4()}/retry")

    assert response.status_code == 401
    assert _error_code(response) == "unauthorized"


# --- Ретраи и резервная модель (P7-01, ADR-0027) ---


@pytest.mark.parametrize("backup_fails", [False, True])
async def test_unavailable_primary_falls_back_to_backup_model(
    db_client: AsyncClient,
    db_session: AsyncSession,
    use_agent: Any,
    monkeypatch: pytest.MonkeyPatch,
    backup_fails: bool,
) -> None:
    monkeypatch.setattr(fallback_llm, "RETRY_DELAY_S", 0.0)
    config = _config("backup")
    config["model"]["fallback"] = {"provider": "anthropic", "name": "backup-model"}
    await _tenant(db_session, "backup", config)
    created = await db_client.post(URL, json={"visitor_id": "v-1"}, headers=_key("backup"))
    conversation_id = UUID(created.json()["conversation_id"])
    unavailable = LLMError("HTTP 503", retryable=True)
    llms = {
        "openai": FakeLLM([unavailable, unavailable]),
        "anthropic": FakeLLM([unavailable if backup_fails else FakeReply(text=ANSWER)]),
    }
    use_agent(
        LoopTurnAgent(lambda p: llms[p], lambda *_: RegistryToolExecutor(ToolRegistry([]))),
    )

    response = await _send(db_client, conversation_id, slug="backup")

    events = [e.model_dump() for _, e in _parse_sse(response.text)]
    [backup_request] = llms["anthropic"].requests
    assert backup_request.model == "backup-model"
    if backup_fails:
        error = next(e for e in events if e["type"] == "error")
        assert (error["data"]["code"], error["data"]["retryable"]) == ("llm_unavailable", True)
        assert events[-1]["data"]["status"] == "failed"
    else:
        assert not any(e["type"] == "error" for e in events)
        texts = "".join(e["data"]["delta"] for e in events if e["type"] == "text_delta")
        assert texts == ANSWER
        assert events[-1]["data"]["status"] == "completed"


# --- Версия AgentConfig, устаревшая по схеме (REVIEW.md: T-1b) ---


async def _make_outdated(session: AsyncSession, config_id: UUID) -> None:
    """Поле, которого нет в схеме: так выглядит версия после смены схемы."""
    record = await session.get(AgentConfigRecord, config_id)
    assert record is not None
    record.config = {**record.config, "legacy_field": True}
    await session.flush()


def _config_outdated(response: Response) -> None:
    assert response.status_code == 500, response.text
    error = HttpError.model_validate(response.json()).error
    assert (error.code, error.retryable) == ("internal", False)
    assert error.message == "конфигурация ассистента неисправна"


async def test_outdated_active_config_does_not_start_conversation(
    db_client: AsyncClient, db_session: AsyncSession, shop: TenantId
) -> None:
    active = await AgentConfigRepository(db_session).get_active(shop)
    assert active is not None
    await _make_outdated(db_session, active.id)

    with structlog.testing.capture_logs() as logs:
        response = await db_client.post(URL, json={"visitor_id": "v-1"}, headers=_key("shop"))

    _config_outdated(response)
    [entry] = [e for e in logs if e["event"] == "agent_config_invalid"]
    assert (entry["tenant_id"], entry["config_id"]) == (str(shop), str(active.id))
    assert entry["problems"] == ["legacy_field"]


async def test_outdated_conversation_config_rejects_turn_without_writing_input(
    db_client: AsyncClient,
    db_session: AsyncSession,
    shop: TenantId,
    conversation_id: UUID,
    use_agent: Any,
) -> None:
    agent = ScriptedAgent()
    use_agent(agent)
    conversation = await ConversationRepository(db_session).find(shop, conversation_id)
    assert conversation is not None
    await _make_outdated(db_session, conversation.agent_config_id)

    first = await _send(db_client, conversation_id)
    second = await _send(db_client, conversation_id)

    _config_outdated(first)
    _config_outdated(second)  # диалог освобождён: не 409 turn_in_progress
    assert agent.requests == []
    assert (await _history(db_client, conversation_id)).messages == []


async def test_outdated_conversation_config_rejects_retry(
    db_client: AsyncClient,
    db_session: AsyncSession,
    shop: TenantId,
    conversation_id: UUID,
    use_agent: Any,
) -> None:
    use_agent(FailingAgent(FLAKY))
    failed = _parse_sse((await _send(db_client, conversation_id)).text)[-1][1].root.message_id
    conversation = await ConversationRepository(db_session).find(shop, conversation_id)
    assert conversation is not None
    await _make_outdated(db_session, conversation.agent_config_id)

    response = await _retry(db_client, conversation_id, str(failed))

    _config_outdated(response)
    # Неудачный ответ не помечен заменённым — его можно повторить, когда конфиг исправят.
    _, assistant = (await _history(db_client, conversation_id)).messages
    assert str(assistant.message_id) == str(failed)
