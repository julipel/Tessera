"""Сроки хранения данных посетителей (ADR-0043): политика тенанта и итог чистки."""

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Protocol

from app.modules.shared.kernel import DomainError, TenantId


@dataclass(frozen=True, slots=True)
class RetentionPolicy:
    """Сроки в днях; умолчания — как в схеме AgentConfig (`retention`)."""

    conversations_days: int = 180
    agent_events_days: int = 90
    leads_days: int = 365

    def conversations_before(self, now: datetime) -> datetime:
        return now - timedelta(days=self.conversations_days)

    def agent_events_before(self, now: datetime) -> datetime:
        # Журнал живёт не дольше диалогов: его каскадом удалит и удаление диалога.
        return now - timedelta(days=min(self.agent_events_days, self.conversations_days))

    def leads_before(self, now: datetime) -> datetime:
        return now - timedelta(days=self.leads_days)


@dataclass(frozen=True, slots=True)
class PurgeResult:
    """Сколько строк удалено напрямую; каскадные удаления (сообщения и т.п.) не считаются."""

    conversations: int = 0
    agent_events: int = 0
    leads: int = 0


class RetentionConfigInvalidError(DomainError):
    """Секция `retention` активного конфига не проходит схему: тенант пропускается —
    удаление необратимо, а умолчания могут оказаться короче сроков тенанта."""


class Purger(Protocol):
    """Удаляет порцию — не больше `limit` строк тенанта старше `before` — и возвращает число."""

    async def __call__(self, tenant_id: TenantId, before: datetime, limit: int) -> int: ...


class Counter(Protocol):
    """Сколько строк тенанта старше `before` удалила бы чистка (dry-run)."""

    async def __call__(self, tenant_id: TenantId, before: datetime) -> int: ...
