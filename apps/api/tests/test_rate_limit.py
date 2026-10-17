"""Лимиты частоты и размера ввода публичного API (P7-04a, ADR-0030)."""

from typing import Any, cast
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
from app.modules.shared.public import (
    REDIS_RETRY_AFTER_S,
    ApiError,
    InMemoryRateLimiter,
    RateLimit,
    RedisRateLimiter,
    TenantId,
    enforce_rate_limits,
)
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


async def test_check_does_not_count_request() -> None:
    limiter = InMemoryRateLimiter(Clock(1_000_040.0))

    assert all([(await limiter.check(RULE, "anna")).allowed for _ in range(5)])
    await limiter.hit(RULE, "anna")
    assert (await limiter.check(RULE, "anna")).allowed
    await limiter.hit(RULE, "anna")
    blocked = await limiter.check(RULE, "anna")  # следующий hit превысил бы лимит
    assert (blocked.allowed, blocked.retry_after_s) == (False, 40)


async def _allowed(limiter: InMemoryRateLimiter, checks: list[tuple[RateLimit, str]]) -> bool:
    try:
        await enforce_rate_limits(limiter, checks)
    except ApiError as e:
        assert e.status_code == 429
        return False
    return True


async def test_requests_rejected_per_ip_do_not_use_up_tenant_limit() -> None:
    """Один IP, упёршийся в свой лимит, не исчерпывает лимит тенанта для остальных."""
    limiter = InMemoryRateLimiter()
    per_ip, per_tenant = RateLimit("ip", 2, 60), RateLimit("tenant", 5, 60)

    def checks(ip: str) -> list[tuple[RateLimit, str]]:
        return [(per_ip, ip), (per_tenant, "shop")]

    flood = [await _allowed(limiter, checks("ip-a")) for _ in range(10)]
    others = [await _allowed(limiter, checks(ip)) for ip in ("ip-b", "ip-c", "ip-d")]

    assert flood.count(True) == 2
    assert others == [True, True, True]  # у тенанта ещё 3 из 5
    assert not await _allowed(limiter, checks("ip-e"))  # а теперь лимит тенанта исчерпан


async def test_first_exceeded_rule_answers_with_its_retry_after() -> None:
    clock = Clock(1000.0)  # 1000 % 60 = 40: окно минуты кончится через 20 с
    limiter = InMemoryRateLimiter(now=clock)
    per_minute, per_hour = RateLimit("minute", 1, 60), RateLimit("hour", 1, 3600)
    await enforce_rate_limits(limiter, [(per_minute, "ip"), (per_hour, "ip")])

    with pytest.raises(ApiError) as error:
        await enforce_rate_limits(limiter, [(per_minute, "ip"), (per_hour, "ip")])

    assert error.value.headers == {"Retry-After": "20"}


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


async def test_unavailable_redis_counts_local_fallback_rules_in_memory() -> None:
    redis = Redis.from_url("redis://127.0.0.2:1/0", socket_connect_timeout=0.2)
    limiter = RedisRateLimiter(redis, Clock(1_000_040.0))
    login = RateLimit("login", limit=2, window_s=60, local_fallback=True)
    try:
        login_decisions = [await limiter.hit(login, "anna") for _ in range(3)]
        chat_decisions = [await limiter.hit(RULE, "ip-1") for _ in range(3)]
    finally:
        await redis.aclose()

    # Вход в админку без Redis не теряет защиту от перебора (REVIEW.md: AC-2), чат — пропускается.
    assert [d.allowed for d in login_decisions] == [True, True, False]
    assert login_decisions[2].retry_after_s == 40
    assert [d.allowed for d in chat_decisions] == [True, True, True]


class CountingRedis:
    """Redis, у которого каждый запрос падает, — со счётчиком обращений."""

    def __init__(self) -> None:
        self.calls = 0

    def pipeline(self, transaction: bool = True) -> Any:
        self.calls += 1
        raise ConnectionError("redis завис")

    async def get(self, key: str) -> None:
        self.calls += 1
        raise ConnectionError("redis завис")


async def test_redis_not_polled_for_a_while_after_failure() -> None:
    """После сбоя Redis следующие правила и запросы не ждут его таймаут (S-19), через
    REDIS_RETRY_AFTER_S — снова пробуем."""
    redis, clock = CountingRedis(), Clock(1_000_000.0)
    limiter = RedisRateLimiter(cast(Redis, redis), clock)
    login = RateLimit("login", limit=1, window_s=60, local_fallback=True)

    with structlog.testing.capture_logs() as logs:
        await enforce_rate_limits(limiter, [(RULE, "ip-1"), (RULE, "tenant-1")])
        assert (await limiter.check(RULE, "ip-1")).allowed
        await limiter.hit(login, "anna")  # запасной счётчик в памяти — и без опроса Redis
        assert not (await limiter.check(login, "anna")).allowed
        assert redis.calls == 1
        clock.now += REDIS_RETRY_AFTER_S
        await limiter.hit(RULE, "ip-1")

    assert redis.calls == 2
    assert [e["event"] for e in logs] == ["rate_limit_unavailable"] * 2


async def test_redis_check_reads_counter_without_incrementing(settings: Any) -> None:
    redis = Redis.from_url(settings.redis_url)
    rule = RateLimit(f"check-{uuid4().hex}", limit=2, window_s=60)
    limiter = RedisRateLimiter(redis, Clock(1_000_040.0))
    try:
        before = [await limiter.check(rule, "anna") for _ in range(3)]
        await limiter.hit(rule, "anna")
        await limiter.hit(rule, "anna")
        after = await limiter.check(rule, "anna")
    finally:
        await redis.delete(*await redis.keys(f"rl:{rule.name}:*"))
        await redis.aclose()

    assert all(d.allowed for d in before)
    assert not after.allowed


async def test_unavailable_redis_check_uses_local_fallback() -> None:
    redis = Redis.from_url("redis://127.0.0.2:1/0", socket_connect_timeout=0.2)
    limiter = RedisRateLimiter(redis, Clock(1_000_040.0))
    login = RateLimit("login", limit=1, window_s=60, local_fallback=True)
    try:
        await limiter.hit(login, "anna")  # неудачный вход учтён в памяти процесса
        login_check = await limiter.check(login, "anna")
        chat_check = await limiter.check(RateLimit("chat", limit=1, window_s=60), "ip-1")
    finally:
        await redis.aclose()

    assert not login_check.allowed
    assert chat_check.allowed


async def test_memory_limiter_drops_counters_of_past_windows() -> None:
    clock = Clock(1_000_040.0)
    limiter = InMemoryRateLimiter(clock, max_keys=3)
    for subject in ("a", "b", "c"):
        await limiter.hit(RULE, subject)
    clock.now += 60  # окно прошло

    await limiter.hit(RULE, "d")

    assert len(limiter) == 1
    assert (await limiter.hit(RULE, "d")).allowed  # счётчик текущего окна не потерян
    assert not (await limiter.hit(RULE, "d")).allowed


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
