"""Публичный интерфейс модуля knowledge — единственная точка входа для других модулей."""

from app.modules.knowledge.application.chunking import ParagraphChunker
from app.modules.knowledge.application.ingestion import run_sync
from app.modules.knowledge.application.sync_requests import request_sync
from app.modules.knowledge.domain.entities import SourceKind, SourceStatus, SyncStatus
from app.modules.knowledge.domain.errors import NoConnectorError
from app.modules.knowledge.domain.ingestion import (
    ChunkDraft,
    DocumentItem,
    EntityItem,
    Listing,
    RawItem,
    RawItemRef,
    content_hash,
)
from app.modules.knowledge.domain.ports import Chunker, SourceConnector, SyncQueue, SyncStore
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
from app.modules.knowledge.infrastructure.sync_queue import SYNC_SOURCE_JOB, ArqSyncQueue
from app.modules.knowledge.infrastructure.sync_store import SqlSyncStore

__all__ = [
    "SYNC_SOURCE_JOB",
    "ArqSyncQueue",
    "ChunkDraft",
    "ChunkRecord",
    "ChunkRepository",
    "Chunker",
    "DocumentItem",
    "DocumentRecord",
    "DocumentRepository",
    "EntityItem",
    "EntityRecord",
    "EntityRepository",
    "Listing",
    "NoConnectorError",
    "ParagraphChunker",
    "RawItem",
    "RawItemRef",
    "SourceConnector",
    "SourceKind",
    "SourceRecord",
    "SourceRepository",
    "SourceStatus",
    "SourceSyncRecord",
    "SourceSyncRepository",
    "SqlSyncStore",
    "SyncQueue",
    "SyncStatus",
    "SyncStore",
    "content_hash",
    "request_sync",
    "run_sync",
]
