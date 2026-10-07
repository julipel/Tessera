"""Лимиты частоты запросов (P7-04a, ADR-0030): фиксированное окно на правило и субъект."""

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True, slots=True)
class RateLimit:
    """Не больше `limit` запросов субъекта за окно `window_s`; `limit` 0 — правило выключено."""

    name: str
    limit: int
    window_s: int


@dataclass(frozen=True, slots=True)
class RateDecision:
    allowed: bool
    retry_after_s: int = 0


ALLOWED = RateDecision(allowed=True)


class RateLimiter(Protocol):
    async def hit(self, rule: RateLimit, subject: str) -> RateDecision:
        """Учесть запрос субъекта и решить, пропустить ли его. Сбой хранилища — пропустить."""
        ...
