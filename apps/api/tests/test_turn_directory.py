"""Отмена хода из любого процесса API (P7-06, ADR-0035).

Процесс API — свой `TurnRegistry`; процессы видят ходы друг друга через общий каталог
(Redis в приложении, `InMemoryTurnDirectory` в тестах)."""

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, cast
from uuid import UUID, uuid4

import anyio
import pytest
from httpx import AsyncClient
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from app.contracts import UserInput
from app.main import create_app, lifespan
from app.modules.agent.public import AgentEvent, AnswerDelta
from app.modules.chat.application.turns import TurnRegistry, TurnStream, start_turn
from app.modules.chat.domain.entities import TurnRequest
from app.modules.chat.infrastructure.repositories import (
    ConversationRepository,
    MessageRepository,
    TenantsAgentConfigs,
    ToolCallRepository,
)
from app.modules.chat.infrastructure.turn_directory import (
    CANCEL_CHANNEL,
    InMemoryTurnDirectory,
    RedisTurnDirectory,
)
from app.modules.shared.public import TenantId
from app.modules.tenants.public import (
    AgentConfigRepository,
    SqlTenantDirectory,
    WidgetKeyRepository,
    hash_widget_key,
)
from app.settings import Settings

WAIT = 5.0  # сломанная отмена оставила бы ход ждать вечно: тест должен упасть, а не зависнуть


@dataclass
class _Conversation:
    tenant_id: TenantId
    id: UUID


@dataclass
class _Turn:
    """Ход в реестре: реестру нужны turn_id, владелец и cancel()."""

    conversation: _Conversation
    turn_id: UUID = field(default_factory=uuid4)
    cancelled: anyio.Event = field(default_factory=anyio.Event)

    def cancel(self) -> None:
        self.cancelled.set()


def _turn() -> _Turn:
    return _Turn(_Conversation(TenantId(uuid4()), uuid4()))


def _stream(turn: _Turn) -> TurnStream:
    return cast(TurnStream, turn)


async def _cancel(registry: TurnRegistry, turn: _Turn, **owner: Any) -> bool:
    tenant_id = owner.get("tenant_id", turn.conversation.tenant_id)
    conversation_id = owner.get("conversation_id", turn.conversation.id)
    return await registry.cancel(tenant_id, conversation_id, turn.turn_id)


# --- Каталог в памяти: два «процесса» ---


async def test_cancel_reaches_turn_in_other_process() -> None:
    directory = InMemoryTurnDirectory()
    owner, other = TurnRegistry(directory), TurnRegistry(directory)
    directory.subscribe(owner.cancel_local)
    directory.subscribe(other.cancel_local)
    turn = _turn()
    await owner.add(_stream(turn))

    assert await _cancel(other, turn)
    assert turn.cancelled.is_set()


async def test_cancel_of_foreign_or_finished_turn_from_other_process_is_rejected() -> None:
    directory = InMemoryTurnDirectory()
    owner, other = TurnRegistry(directory), TurnRegistry(directory)
    directory.subscribe(owner.cancel_local)
    turn = _turn()
    await owner.add(_stream(turn))

    assert not await _cancel(other, turn, tenant_id=TenantId(uuid4()))
    assert not await _cancel(other, turn, conversation_id=uuid4())
    await owner.remove(_stream(turn))
    assert not await _cancel(other, turn)
    assert not turn.cancelled.is_set()


async def test_without_directory_cancel_stays_in_process() -> None:
    owner, other = TurnRegistry(), TurnRegistry()
    turn = _turn()
    await owner.add(_stream(turn))

    assert not await _cancel(other, turn)
    assert await _cancel(owner, turn)


# --- Redis ---


@pytest.fixture
async def redis(settings: Settings) -> AsyncIterator[Redis]:
    client = Redis.from_url(settings.redis_url)
    yield client
    await client.aclose()


async def _wait_for(check: Callable[[], Awaitable[bool]]) -> None:
    # Подписку в Redis видно только запросом (PUBSUB NUMSUB) — опрос.
    with anyio.fail_after(WAIT):
        while not await check():  # noqa: ASYNC110
            await asyncio.sleep(0.02)


async def _subscribers(redis: Redis) -> int:
    [(_, count)] = await redis.pubsub_numsub(CANCEL_CHANNEL)
    return int(count)


def _subscribed(redis: Redis, at_least: int) -> Callable[[], Awaitable[bool]]:
    async def check() -> bool:
        return await _subscribers(redis) >= at_least

    return check


async def test_cancel_through_redis_reaches_turn_in_other_process(
    settings: Settings, redis: Redis
) -> None:
    subscribed = await _subscribers(redis)
    processes = []
    async with anyio.create_task_group() as tg:
        for _ in range(2):
            subscriber = Redis.from_url(settings.redis_url)
            directory = RedisTurnDirectory(redis, subscriber)
            registry = TurnRegistry(directory)
            tg.start_soon(directory.listen, registry.cancel_local)
            processes.append((registry, subscriber))
        await _wait_for(_subscribed(redis, subscribed + 2))
        (owner, _), (other, _) = processes
        turn = _turn()
        await owner.add(_stream(turn))
        assert await redis.ttl(f"turn:{turn.turn_id}") > 0
        # Мусор в канале не роняет подписчика.
        await redis.publish(CANCEL_CHANNEL, "не uuid")

        assert not await _cancel(other, turn, conversation_id=uuid4())
        assert await _cancel(other, turn)
        with anyio.fail_after(WAIT):
            await turn.cancelled.wait()

        await owner.remove(_stream(turn))
        assert not await redis.exists(f"turn:{turn.turn_id}")
        assert not await _cancel(other, turn)
        tg.cancel_scope.cancel()
    for _, subscriber in processes:
        await subscriber.aclose()


async def test_unavailable_redis_keeps_turns_and_local_cancel() -> None:
    # Порт без сервера: соединение отклоняется сразу.
    redis = Redis.from_url("redis://127.0.0.2:1/0", socket_connect_timeout=0.2)
    directory = RedisTurnDirectory(redis, redis)
    owner, other = TurnRegistry(directory), TurnRegistry(directory)
    turn = _turn()
    try:
        await owner.add(_stream(turn))

        assert not await _cancel(other, turn)
        assert await _cancel(owner, turn)
        await owner.remove(_stream(turn))
    finally:
        await redis.aclose()


async def test_app_listens_for_cancels_from_other_processes(
    settings: Settings, redis: Redis
) -> None:
    app = create_app(settings)
    registry: TurnRegistry = app.state.turn_registry
    other = TurnRegistry(RedisTurnDirectory(redis, redis))
    subscribed = await _subscribers(redis)
    turn = _turn()
    async with lifespan(app):
        await _wait_for(_subscribed(redis, subscribed + 1))
        await registry.add(_stream(turn))

        assert await _cancel(other, turn)
        with anyio.fail_after(WAIT):
            await turn.cancelled.wait()
        await registry.remove(_stream(turn))


# --- Ход целиком: стрим в одном процессе, отмена — в другом ---


class GatedAgent:
    def __init__(self) -> None:
        self.first_sent = anyio.Event()

    async def run_turn(self, request: TurnRequest) -> AsyncIterator[AgentEvent]:
        yield AnswerDelta("Подбираю ")
        self.first_sent.set()
        await anyio.sleep_forever()
        yield AnswerDelta("не дойдёт")


@pytest.fixture
async def shop(db_session: AsyncSession) -> TenantId:
    tenant = await SqlTenantDirectory(db_session).create("shop", "shop")
    await WidgetKeyRepository(db_session).add_key(tenant.id, hash_widget_key("wk_shop"), [])
    configs = AgentConfigRepository(db_session)
    draft = await configs.create_draft(
        tenant.id,
        {
            "assistant": {"name": "shop", "greeting": "Привет!", "fallback_message": "Увы."},
            "model": {"primary": {"provider": "openai", "name": "test-model"}},
            "limits": {},
            "prompt": {"tenant": "Ты — консультант."},
            "tools": {},
        },
    )
    await configs.activate(tenant.id, draft.id)
    return tenant.id


async def test_turn_cancelled_from_other_process_ends_interrupted(
    db_session: AsyncSession, db_client: AsyncClient, shop: TenantId
) -> None:
    response = await db_client.post(
        "/v1/conversations", json={"visitor_id": "v-1"}, headers={"X-Widget-Key": "wk_shop"}
    )
    conversation_id = UUID(response.json()["conversation_id"])
    directory = InMemoryTurnDirectory()
    owner, other = TurnRegistry(directory), TurnRegistry(directory)
    directory.subscribe(owner.cancel_local)
    agent = GatedAgent()
    turn = await start_turn(
        shop,
        conversation_id,
        uuid4(),
        UserInput.model_validate({"type": "text", "text": "Нужен подарок"}),
        ConversationRepository(db_session),
        MessageRepository(db_session),
        ToolCallRepository(db_session),
        TenantsAgentConfigs(db_session),
        agent,
        db_session.commit,
        owner,
    )
    events = turn.events()
    assert (await anext(events)).root.type == "turn_started"
    assert (await anext(events)).root.type == "text_delta"

    assert await other.cancel(shop, conversation_id, turn.turn_id)
    with anyio.fail_after(WAIT):
        rest = [e.model_dump() async for e in events]

    assert [(e["type"], e["data"]) for e in rest] == [
        ("text_done", {"block_id": "b1"}),
        ("done", {"status": "interrupted", "usage": None}),
    ]
    await turn.close()
    assert turn.turn_id not in owner
    assert not await other.cancel(shop, conversation_id, turn.turn_id)
