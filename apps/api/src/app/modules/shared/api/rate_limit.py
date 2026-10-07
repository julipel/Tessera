"""Проверка лимитов частоты в роутерах: 429 `rate_limited` с `Retry-After` (ADR-0030)."""

from collections.abc import Sequence

import structlog
from fastapi import Request, status

from app.modules.shared.api.errors import ApiError
from app.modules.shared.domain.rate_limit import RateLimit, RateLimiter

logger = structlog.get_logger(__name__)


def client_ip(request: Request) -> str:
    """IP клиента; за обратным прокси — из X-Forwarded-For, если uvicorn запущен
    с `--proxy-headers --forwarded-allow-ips` (иначе это адрес прокси)."""
    return request.client.host if request.client else "unknown"


async def enforce_rate_limits(
    limiter: RateLimiter, checks: Sequence[tuple[RateLimit, str]]
) -> None:
    """Учесть запрос во всех правилах; превышено любое — 429 с наибольшим `Retry-After`."""
    retry_after = 0
    exceeded: list[str] = []
    for rule, subject in checks:
        decision = await limiter.hit(rule, subject)
        if not decision.allowed:
            exceeded.append(rule.name)
            retry_after = max(retry_after, decision.retry_after_s)
    if exceeded:
        logger.warning("rate_limited", rules=exceeded, retry_after_s=retry_after)
        raise ApiError(
            status.HTTP_429_TOO_MANY_REQUESTS,
            "rate_limited",
            "слишком много запросов, повторите позже",
            headers={"Retry-After": str(retry_after)},
        )
