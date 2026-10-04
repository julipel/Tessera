"""Порты зависимостей встроенных инструментов (реализации — в других модулях, подключает
тот, кто собирает агент)."""

from typing import Protocol

from app.modules.knowledge.kernel import ChunkHit
from app.modules.shared.kernel import TenantId


class KnowledgeSearcher(Protocol):
    """Поиск по знаниям тенанта (knowledge `KnowledgeSearch`). Ошибки — `EmbeddingError`,
    `VectorIndexError` из knowledge.kernel."""

    async def search(
        self, tenant_id: TenantId, query: str, *, top_k: int, rerank: bool
    ) -> list[ChunkHit]: ...
