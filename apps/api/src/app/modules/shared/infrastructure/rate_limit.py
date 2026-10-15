"""Лимитеры частоты: Redis (общий для процессов API) и в памяти (тесты)."""

import math
import time
from collections.abc import Callable

import structlog
from redis.asyncio import Redis
from redis.exceptions import RedisError

from app.modules.shared.domain.rate_limit import ALLOWED, RateDecision, RateLimit

logger = structlog.get_logger(__name__)


def _window(rule: RateLimit, now: float) -> tuple[int, int]:
    """Номер окна и сколько секунд до его конца (не меньше 1)."""
    index = int(now // rule.window_s)
    return index, max(1, math.ceil((index + 1) * rule.window_s - now))


class RedisRateLimiter:
    """Фиксированное окно: `INCR` счётчика `rl:<правило>:<субъект>:<окно>` и `EXPIRE` на окно.
    Redis недоступен — запрос пропускается с предупреждением в лог: лимит не должен ронять
    чат (ADR-0030). Правила с `local_fallback` (вход в админку) тогда считаются в памяти
    процесса: перебор пароля упирается в лимит и без Redis, вход не закрывается совсем."""

    def __init__(self, redis: Redis, now: Callable[[], float] = time.time) -> None:
        self._redis = redis
        self._now = now
        self._fallback = InMemoryRateLimiter(now)

    async def hit(self, rule: RateLimit, subject: str) -> RateDecision:
        if rule.limit <= 0:
            return ALLOWED
        index, retry_after = _window(rule, self._now())
        key = f"rl:{rule.name}:{subject}:{index}"
        try:
            async with self._redis.pipeline(transaction=True) as pipe:
                pipe.incr(key)
                pipe.expire(key, rule.window_s + 1)
                count, _ = await pipe.execute()
        except (RedisError, OSError) as e:
            logger.warning(
                "rate_limit_unavailable",
                rule=rule.name,
                error=str(e),
                local_fallback=rule.local_fallback,
            )
            return await self._fallback.hit(rule, subject) if rule.local_fallback else ALLOWED
        if int(count) > rule.limit:
            return RateDecision(allowed=False, retry_after_s=retry_after)
        return ALLOWED

    async def check(self, rule: RateLimit, subject: str) -> RateDecision:
        if rule.limit <= 0:
            return ALLOWED
        index, retry_after = _window(rule, self._now())
        try:
            count = await self._redis.get(f"rl:{rule.name}:{subject}:{index}")
        except (RedisError, OSError) as e:
            logger.warning(
                "rate_limit_unavailable",
                rule=rule.name,
                error=str(e),
                local_fallback=rule.local_fallback,
            )
            return await self._fallback.check(rule, subject) if rule.local_fallback else ALLOWED
        if count is not None and int(count) >= rule.limit:
            return RateDecision(allowed=False, retry_after_s=retry_after)
        return ALLOWED


class InMemoryRateLimiter:
    """Те же окна в памяти процесса — для тестов и как запасной счётчик `RedisRateLimiter`
    (между процессами не работает). Счётчики прошедших окон удаляются, когда их больше
    `max_keys`: память не растёт с числом субъектов."""

    def __init__(self, now: Callable[[], float] = time.time, *, max_keys: int = 10_000) -> None:
        self._now = now
        self._max_keys = max_keys
        self._counts: dict[str, tuple[int, float]] = {}  # ключ → (счётчик, конец окна)

    async def hit(self, rule: RateLimit, subject: str) -> RateDecision:
        if rule.limit <= 0:
            return ALLOWED
        now = self._now()
        index, retry_after = _window(rule, now)
        key = f"{rule.name}:{subject}:{index}"
        if key not in self._counts and len(self._counts) >= self._max_keys:
            self._counts = {k: v for k, v in self._counts.items() if v[1] > now}
        count = self._counts.get(key, (0, 0.0))[0] + 1
        self._counts[key] = (count, (index + 1) * rule.window_s)
        if count > rule.limit:
            return RateDecision(allowed=False, retry_after_s=retry_after)
        return ALLOWED

    async def check(self, rule: RateLimit, subject: str) -> RateDecision:
        if rule.limit <= 0:
            return ALLOWED
        index, retry_after = _window(rule, self._now())
        count = self._counts.get(f"{rule.name}:{subject}:{index}", (0, 0.0))[0]
        if count >= rule.limit:
            return RateDecision(allowed=False, retry_after_s=retry_after)
        return ALLOWED

    def __len__(self) -> int:
        return len(self._counts)
