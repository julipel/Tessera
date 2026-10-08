"""Публичный интерфейс модуля knowledge — единственная точка входа для других модулей."""

from app.modules.knowledge.api.admin_router import router as admin_router
from app.modules.knowledge.application.chunking import MarkdownChunker
from app.modules.knowledge.application.ingestion import run_sync
from app.modules.knowledge.application.search import KnowledgeSearch
from app.modules.knowledge.application.source_seed import parse_source_declarations, seed_sources
from app.modules.knowledge.application.sync_requests import request_sync, run_sync_now
from app.modules.knowledge.domain.catalog import (
    AttributeFilter,
    CatalogEntity,
    CatalogFilters,
    CatalogPage,
    CatalogQuery,
    CatalogSort,
)
from app.modules.knowledge.domain.entities import SourceKind, SourceStatus, SyncStatus
from app.modules.knowledge.domain.errors import (
    CatalogError,
    EmbeddingError,
    InvalidSourceDeclarationError,
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
    SourceFileStore,
    SourceRegistry,
    SyncQueue,
    SyncStore,
)
from app.modules.knowledge.domain.source_admin import SourceFileLimits
from app.modules.knowledge.domain.source_seed import (
    MirrorStats,
    SeedAction,
    SeededSource,
    SeedSourcesResult,
    SourceDeclaration,
)
from app.modules.knowledge.infrastructure.catalog import SqlCatalog
from app.modules.knowledge.infrastructure.database_connector import (
    DatabaseConnector,
    DatabaseSourceConfig,
    DatabaseSourceError,
    parse_networks,
)
from app.modules.knowledge.infrastructure.file_connector import FileConnector
from app.modules.knowledge.infrastructure.http_api_connector import (
    HttpApiConnector,
    HttpApiSourceConfig,
    HttpApiSourceError,
)
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
from app.modules.knowledge.infrastructure.source_configs import validate_source_config
from app.modules.knowledge.infrastructure.source_files import LocalSourceFileStore, SourceFileError
from app.modules.knowledge.infrastructure.source_secrets import SourceSecretError, SourceSecrets
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
    "AttributeFilter",
    "CatalogEntity",
    "CatalogError",
    "CatalogFilters",
    "CatalogPage",
    "CatalogQuery",
    "CatalogSort",
    "ChunkDraft",
    "ChunkHit",
    "ChunkIndex",
    "ChunkRecord",
    "ChunkRepository",
    "Chunker",
    "DatabaseConnector",
    "DatabaseSourceConfig",
    "DatabaseSourceError",
    "DocumentItem",
    "DocumentRecord",
    "DocumentRepository",
    "Embedder",
    "EmbeddingError",
    "EntityItem",
    "EntityRecord",
    "EntityRepository",
    "FileConnector",
    "HttpApiConnector",
    "HttpApiSourceConfig",
    "HttpApiSourceError",
    "HttpReranker",
    "IndexedChunk",
    "IndexedDocument",
    "InvalidSourceDeclarationError",
    "KnowledgeSearch",
    "Listing",
    "LocalSourceFileStore",
    "MarkdownChunker",
    "MirrorStats",
    "NoConnectorError",
    "OpenAIEmbedder",
    "QdrantChunkIndex",
    "RawItem",
    "RawItemRef",
    "RerankError",
    "Reranker",
    "SeedAction",
    "SeedSourcesResult",
    "SeededSource",
    "SourceConnector",
    "SourceDeclaration",
    "SourceFileError",
    "SourceFileLimits",
    "SourceFileStore",
    "SourceKind",
    "SourceRecord",
    "SourceRegistry",
    "SourceRepository",
    "SourceSecretError",
    "SourceSecrets",
    "SourceSpec",
    "SourceStatus",
    "SourceSyncRecord",
    "SourceSyncRepository",
    "SqlCatalog",
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
    "admin_router",
    "build_web_client",
    "content_hash",
    "create_http_reranker",
    "create_openai_embedder",
    "create_qdrant_index",
    "parse_networks",
    "parse_source_declarations",
    "request_sync",
    "run_sync",
    "run_sync_now",
    "seed_sources",
    "validate_source_config",
]
