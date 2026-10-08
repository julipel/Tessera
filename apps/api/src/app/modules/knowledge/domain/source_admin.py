"""Источники в админке (ADR-0037): обзор источника и его синхронизаций."""

from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID

from app.modules.knowledge.domain.entities import SourceKind, SourceOrigin, SourceStatus, SyncStatus

# Виды, которые создаются в админке. http_api и database ссылаются на секреты из env,
# общие для всех тенантов (ADR-0017), — они объявляются только в YAML тенанта.
ADMIN_SOURCE_KINDS = frozenset({SourceKind.WEBSITE, SourceKind.FILE, SourceKind.TABLE})

# Сколько последних синхронизаций источника показывает админка.
RECENT_SYNCS = 20


@dataclass(frozen=True, slots=True)
class SyncRun:
    """`stats` — None, пока синхронизация не закончилась."""

    id: UUID
    status: SyncStatus
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None
    stats: dict[str, Any] | None
    error: str | None


@dataclass(frozen=True, slots=True)
class SourceOverview:
    """Источник со счётчиками данных и последней синхронизацией (любого статуса)."""

    id: UUID
    name: str | None
    kind: SourceKind
    origin: SourceOrigin
    status: SourceStatus
    created_at: datetime
    config: dict[str, Any]
    documents: int
    entities: int
    last_sync: SyncRun | None
