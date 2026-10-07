"""Лимиты частоты и размера ввода публичного API (P7-04a, ADR-0030)."""

from typing import Any
from uuid import UUID, uuid4

import pytest
import structlog
from fastapi import FastAPI
from httpx import AsyncClient, Response
from redis.asyncio import Redis
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.contracts import HttpError
from app.modules.agent.public import AgentEvent, AnswerDelta, LLMError
from app.modules.chat.api.router import MAX_TURN_BODY_BYTES
from app.modules.chat.infrastructure.models import MessageRecord
from app.modules.shared.public import InMemoryRateLimiter, RateLimit, RedisRateLimiter, TenantId
from test_agent_events import ScriptedAgent, _sse, _tenant
from test_agent_events import use_agent as use_agent

URL = "/v1/conversations"
RULE = RateLimit("test", limit=2, window_s=60)


class Clock:
    def __init__(self, now: float) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


async def test_window_limits_and_resets() -> None:
    clock = Clock(1_000_040.0)  # 20 с от начала минутного окна
    limiter = InMemoryRateLimiter(clock)

    decisions = [await limiter.hit(RULE, "ip-1") for _ in range(3)]

    assert [d.allowed for d in decisions] == [True, True, False]
    assert decisions[2].retry_after_s == 40
    assert (await limiter.hit(RULE, "ip-2")).allowed  # другой субъект — свой счётчик
    clock.now += 40
    assert (await limiter.hit(RULE, "ip-1")).allowed  # новое окно


async def test_zero_limit_disables_rule() -> None:
    limiter = InMemoryRateLimiter()
    rule = RateLimit("off", limit=0, window_s=60)

    assert all([(await limiter.hit(rule, "ip")).allowed for _ in range(5)])


async def test_redis_limiter_counts_in_redis(settings: Any) -> None:
    redis = Redis.from_url(settings.redis_url)
    rule = RateLimit(f"test-{uuid4().hex}", limit=2, window_s=60)
    limiter = RedisRateLimiter(redis, Clock(1_000_040.0))
    try:
        decisions = [await limiter.hit(rule, "ip-1") for _ in range(3)]
        keys = await redis.keys(f"rl:{rule.name}:*")
        ttl = await redis.ttl(keys[0])
    finally:
        await redis.delete(*await redis.keys(f"rl:{rule.name}:*"))
        await redis.aclose()

    assert [d.allowed for d in decisions] == [True, True, False]
    assert decisions[2].retry_after_s == 40
    assert len(keys) == 1 and 0 < ttl <= 61


async def test_unavailable_redis_lets_requests_through() -> None:
    # 127.0.0.2: в WSL (mirrored) закрытый порт 127.0.0.1 не отказывает сразу, а висит.
    redis = Redis.from_url("redis://127.0.0.2:1/0", socket_connect_timeout=0.2)
    limiter = RedisRateLimiter(redis)
    try:
        with structlog.testing.capture_logs() as logs:
            decision = await limiter.hit(RULE, "ip-1")
    finally:
        await redis.aclose()

    assert decision.allowed
    assert [e["event"] for e in logs] == ["rate_limit_unavailable"]


# --- HTTP ---


def _limits(app: FastAPI, **values: int) -> None:
    app.state.settings = app.state.settings.model_copy(update=values)


@pytest.fixture
async def shop(db_session: AsyncSession) -> TenantId:
    return await _tenant(db_session, "shop")


async def _create(client: AsyncClient, slug: str = "shop") -> Response:
    return await client.post(
        URL, json={"visitor_id": "v-1"}, headers={"X-Widget-Key": f"wk_{slug}"}
    )


async def _send(
    client: AsyncClient, conversation_id: str, user_input: dict[str, Any] | None = None
) -> Response:
    return await client.post(
        f"{URL}/{conversation_id}/messages",
        json={
            "client_message_id": str(uuid4()),
            "input": user_input or {"type": "text", "text": "Привет"},
        },
        headers={"X-Widget-Key": "wk_shop"},
    )


def _assert_rate_limited(response: Response) -> None:
    assert response.status_code == 429
    error = HttpError.model_validate(response.json()).error
    assert (error.code, error.retryable) == ("rate_limited", True)
    assert int(response.headers["Retry-After"]) >= 1


async def test_turns_are_limited_per_ip(
    app: FastAPI, db_client: AsyncClient, shop: TenantId, use_agent: Any
) -> None:
    answer: list[AgentEvent] = [AnswerDelta("Да.")]
    use_agent(ScriptedAgent(answer, answer, answer))
    _limits(app, rate_turns_per_ip_per_min=2)
    conversation_id = (await _create(db_client)).json()["conversation_id"]

    responses = [await _send(db_client, conversation_id) for _ in range(3)]

    assert [r.status_code for r in responses] == [200, 200, 429]
    _assert_rate_limited(responses[2])


async def test_turns_are_limited_per_tenant_and_retry_counts(
    app: FastAPI, db_client: AsyncClient, shop: TenantId, use_agent: Any
) -> None:
    use_agent(ScriptedAgent(LLMError("503", retryable=True)))
    _limits(app, rate_turns_per_tenant_per_min=1)
    conversation_id = (await _create(db_client)).json()["conversation_id"]

    failed = await _send(db_client, conversation_id)
    message_id = _sse(failed)[-1]["message_id"]
    retry = await db_client.post(
        f"{URL}/{conversation_id}/messages/{message_id}/retry", headers={"X-Widget-Key": "wk_shop"}
    )

    assert failed.status_code == 200
    _assert_rate_limited(retry)


async def test_conversations_are_limited_per_ip(
    app: FastAPI, db_client: AsyncClient, shop: TenantId
) -> None:
    _limits(app, rate_conversations_per_ip_per_hour=1)

    first, second = await _create(db_client), await _create(db_client)

    assert first.status_code == 201
    _assert_rate_limited(second)


async def test_unknown_key_is_rejected_before_turn_limit(
    app: FastAPI, db_client: AsyncClient, shop: TenantId
) -> None:
    _limits(app, rate_turns_per_ip_per_min=1)
    conversation_id = (await _create(db_client)).json()["conversation_id"]

    for _ in range(3):
        response = await db_client.post(
            f"{URL}/{conversation_id}/messages",
            json={"client_message_id": str(uuid4()), "input": {"type": "text", "text": "x"}},
            headers={"X-Widget-Key": "wk_nope"},
        )
        assert response.status_code == 401


async def _messages(session: AsyncSession, conversation_id: str) -> int:
    count = await session.scalar(
        select(func.count()).where(MessageRecord.conversation_id == UUID(conversation_id))
    )
    return int(count or 0)


async def test_too_long_text_and_body_are_rejected(
    db_client: AsyncClient, db_session: AsyncSession, shop: TenantId
) -> None:
    conversation_id = (await _create(db_client)).json()["conversation_id"]
    huge_payload = {"blob": "x" * MAX_TURN_BODY_BYTES}

    too_long = await _send(db_client, conversation_id, {"type": "text", "text": "я" * 4001})
    too_big = await _send(
        db_client,
        conversation_id,
        {"type": "action", "action_id": "pick", "payload": huge_payload},
    )

    for response in (too_long, too_big):
        assert response.status_code == 422
        assert HttpError.model_validate(response.json()).error.code == "invalid_input"
    assert await _messages(db_session, conversation_id) == 0
