"""Лимиты частоты запросов (P7-04a, ADR-0030): фиксированное окно на правило и субъект."""

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True, slots=True)
class RateLimit:
    """Не больше `limit` запросов субъекта за окно `window_s`; `limit` 0 — правило выключено.
    `local_fallback` — если общее хранилище недоступно, считать в памяти процесса, а не пропускать
    (защита от перебора пароля, ADR-0030)."""

    name: str
    limit: int
    window_s: int
    local_fallback: bool = False


@dataclass(frozen=True, slots=True)
class RateDecision:
    allowed: bool
    retry_after_s: int = 0


ALLOWED = RateDecision(allowed=True)


class RateLimiter(Protocol):
    async def hit(self, rule: RateLimit, subject: str) -> RateDecision:
        """Учесть запрос субъекта и решить, пропустить ли его. Сбой хранилища — пропустить
        (правило с `local_fallback` — считать в памяти процесса)."""
        ...

    async def check(self, rule: RateLimit, subject: str) -> RateDecision:
        """Исчерпан ли лимит субъекта в текущем окне — без учёта запроса: запрос учитывают
        потом `hit`, если он того стоит (неудачный вход, AC-3). Сбой хранилища — как у
        `hit`."""
        ...
