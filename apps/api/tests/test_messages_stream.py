"""POST /v1/conversations/{id}/messages: SSE-стрим хода с эхо-агентом (docs/contracts.md §2)."""

import asyncio
import json
from collections.abc import AsyncIterator, Iterator
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
from app.modules.chat.api.router import get_turn_agent
from app.modules.chat.api.sse import sse_stream
from app.modules.chat.application.turns import TurnRegistry, start_turn
from app.modules.chat.domain.entities import AgentTextDelta, TurnRequest
from app.modules.chat.domain.ports import TurnAgent
from app.modules.chat.infrastructure.echo_agent import EchoAgent
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
TEXT = {"type": "text", "text": "Нужен подарок маме"}


async def _tenant(session: AsyncSession, slug: str) -> TenantId:
    tenant = await SqlTenantDirectory(session).create(slug, slug)
    await WidgetKeyRepository(session).add_key(tenant.id, hash_widget_key(f"wk_{slug}"), [])
    configs = AgentConfigRepository(session)
    draft = await configs.create_draft(tenant.id, {"assistant": {"name": slug}})
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


class FailingAgent:
    async def run_turn(self, request: TurnRequest) -> AsyncIterator[AgentTextDelta]:
        yield AgentTextDelta("Сейчас ")
        raise RuntimeError("агент упал")


@pytest.fixture
def use_agent(app: FastAPI) -> Iterator[Any]:
    def use(agent: TurnAgent) -> None:
        app.dependency_overrides[get_turn_agent] = lambda: agent

    use(EchoAgent())
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
    assert "".join(d.delta for d in deltas) == "Вы написали: Нужен подарок маме"
    assert {d.block_id for d in deltas} == {"b1"}
    assert events[-1][1].model_dump()["data"]["status"] == "completed"


async def test_turn_is_saved_to_history(db_client: AsyncClient, conversation_id: UUID) -> None:
    events = _parse_sse((await _send(db_client, conversation_id)).text)

    user, assistant = (await _history(db_client, conversation_id)).messages
    assert user.input is not None and user.input.model_dump() == TEXT
    assert (assistant.role, assistant.status) == ("assistant", "completed")
    assert str(assistant.message_id) == events[-1][1].root.message_id
    assert [b.root.model_dump() for b in assistant.blocks] == [
        {"type": "text", "block_id": "b1", "text": "Вы написали: Нужен подарок маме"}
    ]


@pytest.mark.parametrize(
    ("user_input", "echo"),
    [
        ({"type": "action", "action_id": "select_product"}, "Нажата кнопка: select_product"),
        ({"type": "form_submit", "form_id": "f_1", "values": {}}, "Отправлена форма: f_1"),
    ],
)
async def test_non_text_input_is_echoed(
    db_client: AsyncClient, conversation_id: UUID, user_input: dict[str, Any], echo: str
) -> None:
    events = _parse_sse((await _send(db_client, conversation_id, user_input)).text)

    deltas = [e.model_dump()["data"]["delta"] for _, e in events if e.root.type == "text_delta"]
    assert "".join(deltas) == echo


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

    async def run_turn(self, request: TurnRequest) -> AsyncIterator[AgentTextDelta]:
        self.turn_id = request.turn_id
        yield AgentTextDelta("Подбираю ")
        self.first_sent.set()
        await anyio.sleep_forever()
        yield AgentTextDelta("не дойдёт")


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

    async def run_turn(self, request: TurnRequest) -> AsyncIterator[AgentTextDelta]:
        self.calls += 1
        yield AgentTextDelta("ответ")


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
