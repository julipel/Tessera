"""Публичный интерфейс модуля knowledge — единственная точка входа для других модулей."""

from app.modules.knowledge.application.chunking import MarkdownChunker
from app.modules.knowledge.application.ingestion import run_sync
from app.modules.knowledge.application.search import KnowledgeSearch
from app.modules.knowledge.application.sync_requests import request_sync
from app.modules.knowledge.domain.entities import SourceKind, SourceStatus, SyncStatus
from app.modules.knowledge.domain.errors import (
    EmbeddingError,
    NoConnectorError,
    RerankError,
    VectorIndexError,
)
from app.modules.knowledge.domain.indexing import ChunkHit, IndexedChunk, IndexedDocument
from app.modules.knowledge.domain.ingestion import (
    ChunkDraft,
    DocumentItem,
    EntityItem,
    Listing,
    RawItem,
    RawItemRef,
    SourceSpec,
    content_hash,
)
from app.modules.knowledge.domain.ports import (
    Chunker,
    ChunkIndex,
    Embedder,
    Reranker,
    SourceConnector,
    SyncQueue,
    SyncStore,
)
from app.modules.knowledge.infrastructure.file_connector import FileConnector
from app.modules.knowledge.infrastructure.http_reranker import HttpReranker, create_http_reranker
from app.modules.knowledge.infrastructure.models import (
    ChunkRecord,
    DocumentRecord,
    EntityRecord,
    SourceRecord,
    SourceSyncRecord,
)
from app.modules.knowledge.infrastructure.openai_embedder import (
    OpenAIEmbedder,
    create_openai_embedder,
)
from app.modules.knowledge.infrastructure.qdrant_index import (
    QdrantChunkIndex,
    create_qdrant_index,
)
from app.modules.knowledge.infrastructure.repositories import (
    ChunkRepository,
    DocumentRepository,
    EntityRepository,
    SourceRepository,
    SourceSyncRepository,
)
from app.modules.knowledge.infrastructure.source_files import SourceFileError
from app.modules.knowledge.infrastructure.sync_queue import SYNC_SOURCE_JOB, ArqSyncQueue
from app.modules.knowledge.infrastructure.sync_store import SqlSyncStore
from app.modules.knowledge.infrastructure.table_connector import (
    TableConnector,
    TableSourceConfig,
    TableSourceError,
)
from app.modules.knowledge.infrastructure.web_client import WebClient, build_web_client
from app.modules.knowledge.infrastructure.website_connector import (
    WebsiteConnector,
    WebsiteSourceConfig,
    WebsiteSourceError,
)

__all__ = [
    "SYNC_SOURCE_JOB",
    "ArqSyncQueue",
    "ChunkDraft",
    "ChunkHit",
    "ChunkIndex",
    "ChunkRecord",
    "ChunkRepository",
    "Chunker",
    "DocumentItem",
    "DocumentRecord",
    "DocumentRepository",
    "Embedder",
    "EmbeddingError",
    "EntityItem",
    "EntityRecord",
    "EntityRepository",
    "FileConnector",
    "HttpReranker",
    "IndexedChunk",
    "IndexedDocument",
    "KnowledgeSearch",
    "Listing",
    "MarkdownChunker",
    "NoConnectorError",
    "OpenAIEmbedder",
    "QdrantChunkIndex",
    "RawItem",
    "RawItemRef",
    "RerankError",
    "Reranker",
    "SourceConnector",
    "SourceFileError",
    "SourceKind",
    "SourceRecord",
    "SourceRepository",
    "SourceSpec",
    "SourceStatus",
    "SourceSyncRecord",
    "SourceSyncRepository",
    "SqlSyncStore",
    "SyncQueue",
    "SyncStatus",
    "SyncStore",
    "TableConnector",
    "TableSourceConfig",
    "TableSourceError",
    "VectorIndexError",
    "WebClient",
    "WebsiteConnector",
    "WebsiteSourceConfig",
    "WebsiteSourceError",
    "build_web_client",
    "content_hash",
    "create_http_reranker",
    "create_openai_embedder",
    "create_qdrant_index",
    "request_sync",
    "run_sync",
]
