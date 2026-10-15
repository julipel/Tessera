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
    """Учесть запрос в правилах по порядку; первое превышенное — 429 с его `Retry-After`.

    `checks` — от узкого правила к широкому (IP → тенант): после отказа следующие правила
    не учитывают запрос, иначе отклонённые запросы одного IP исчерпали бы лимит тенанта
    для всех его посетителей.
    """
    for rule, subject in checks:
        decision = await limiter.hit(rule, subject)
        if not decision.allowed:
            logger.warning("rate_limited", rule=rule.name, retry_after_s=decision.retry_after_s)
            raise ApiError(
                status.HTTP_429_TOO_MANY_REQUESTS,
                "rate_limited",
                "слишком много запросов, повторите позже",
                headers={"Retry-After": str(decision.retry_after_s)},
            )
