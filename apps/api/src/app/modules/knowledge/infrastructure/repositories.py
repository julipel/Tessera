"""Репозитории модуля knowledge.

Пока только базовые операции TenantRepository; upsert по external_id, замена чанков и
дедупликация по content_hash появятся вместе с ingestion (P4-02).
"""

from app.modules.knowledge.infrastructure.models import (
    ChunkRecord,
    DocumentRecord,
    EntityRecord,
    SourceRecord,
    SourceSyncRecord,
)
from app.modules.shared.public import TenantRepository


class SourceRepository(TenantRepository[SourceRecord]):
    model = SourceRecord


class SourceSyncRepository(TenantRepository[SourceSyncRecord]):
    model = SourceSyncRecord


class DocumentRepository(TenantRepository[DocumentRecord]):
    model = DocumentRecord


class ChunkRepository(TenantRepository[ChunkRecord]):
    model = ChunkRecord


class EntityRepository(TenantRepository[EntityRecord]):
    model = EntityRecord
