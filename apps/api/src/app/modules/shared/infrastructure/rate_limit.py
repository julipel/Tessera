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
    чат (ADR-0030)."""

    def __init__(self, redis: Redis, now: Callable[[], float] = time.time) -> None:
        self._redis = redis
        self._now = now

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
            logger.warning("rate_limit_unavailable", rule=rule.name, error=str(e))
            return ALLOWED
        if int(count) > rule.limit:
            return RateDecision(allowed=False, retry_after_s=retry_after)
        return ALLOWED


class InMemoryRateLimiter:
    """Те же окна в памяти процесса — для тестов (между процессами не работает)."""

    def __init__(self, now: Callable[[], float] = time.time) -> None:
        self._now = now
        self._counts: dict[str, int] = {}

    async def hit(self, rule: RateLimit, subject: str) -> RateDecision:
        if rule.limit <= 0:
            return ALLOWED
        index, retry_after = _window(rule, self._now())
        key = f"{rule.name}:{subject}:{index}"
        self._counts[key] = self._counts.get(key, 0) + 1
        if self._counts[key] > rule.limit:
            return RateDecision(allowed=False, retry_after_s=retry_after)
        return ALLOWED
