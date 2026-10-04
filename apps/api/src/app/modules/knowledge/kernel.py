"""Типы knowledge без фреймворков — для модулей, чей импорт `public.py` протащил бы
FastAPI/SQLAlchemy (ADR-0008): результат поиска и ошибки поиска по знаниям."""

from app.modules.knowledge.domain.errors import EmbeddingError, RerankError, VectorIndexError
from app.modules.knowledge.domain.indexing import ChunkHit

__all__ = ["ChunkHit", "EmbeddingError", "RerankError", "VectorIndexError"]
