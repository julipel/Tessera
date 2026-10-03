"""Публичный интерфейс модуля knowledge — единственная точка входа для других модулей."""

from app.modules.knowledge.domain.entities import SourceKind, SourceStatus, SyncStatus
from app.modules.knowledge.infrastructure.models import (
    ChunkRecord,
    DocumentRecord,
    EntityRecord,
    SourceRecord,
    SourceSyncRecord,
)
from app.modules.knowledge.infrastructure.repositories import (
    ChunkRepository,
    DocumentRepository,
    EntityRepository,
    SourceRepository,
    SourceSyncRepository,
)

__all__ = [
    "ChunkRecord",
    "ChunkRepository",
    "DocumentRecord",
    "DocumentRepository",
    "EntityRecord",
    "EntityRepository",
    "SourceKind",
    "SourceRecord",
    "SourceRepository",
    "SourceStatus",
    "SourceSyncRecord",
    "SourceSyncRepository",
    "SyncStatus",
]
