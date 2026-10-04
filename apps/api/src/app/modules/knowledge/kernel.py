"""Типы knowledge без фреймворков — для модулей, чей импорт `public.py` протащил бы
FastAPI/SQLAlchemy (ADR-0008): результаты и ошибки поиска по знаниям и по каталогу."""

from app.modules.knowledge.domain.catalog import (
    AttributeFilter,
    AttributeValue,
    CatalogEntity,
    CatalogFilters,
    CatalogPage,
    CatalogQuery,
    CatalogSort,
)
from app.modules.knowledge.domain.errors import (
    CatalogError,
    EmbeddingError,
    RerankError,
    VectorIndexError,
)
from app.modules.knowledge.domain.indexing import ChunkHit

__all__ = [
    "AttributeFilter",
    "AttributeValue",
    "CatalogEntity",
    "CatalogError",
    "CatalogFilters",
    "CatalogPage",
    "CatalogQuery",
    "CatalogSort",
    "ChunkHit",
    "EmbeddingError",
    "RerankError",
    "VectorIndexError",
]
