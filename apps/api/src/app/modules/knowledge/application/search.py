"""Поиск по знаниям тенанта (architecture.md §8): эмбеддинг запроса → гибридный поиск в
индексе (dense + BM25, RRF) → опциональный реранкинг top-k.

С реранкингом из индекса берётся больше кандидатов, реранкер выбирает из них `top_k`.
Сбой реранкера не ломает поиск: остаётся порядок RRF (предупреждение в лог). Сбои эмбеддера
и индекса (`EmbeddingError`, `VectorIndexError`) уходят вызывающему.
"""

from collections.abc import Sequence

import structlog

from app.modules.knowledge.domain.errors import RerankError
from app.modules.knowledge.domain.indexing import ChunkHit
from app.modules.knowledge.domain.ports import ChunkIndex, Embedder, Reranker
from app.modules.shared.kernel import TenantId

logger = structlog.get_logger(__name__)

RERANK_CANDIDATES_MIN = 20
RERANK_CANDIDATES_FACTOR = 3


class KnowledgeSearch:
    """`reranker` — None, если реранкинг в окружении не настроен: тогда `rerank=True`
    в запросе игнорируется."""

    def __init__(
        self, embedder: Embedder, index: ChunkIndex, reranker: Reranker | None = None
    ) -> None:
        self._embedder = embedder
        self._index = index
        self._reranker = reranker

    async def search(
        self, tenant_id: TenantId, query: str, *, top_k: int, rerank: bool
    ) -> list[ChunkHit]:
        if top_k < 1:
            raise ValueError("top_k must be positive")
        [dense] = await self._embedder.embed([query])
        reranker = self._reranker if rerank else None
        limit = (
            max(top_k * RERANK_CANDIDATES_FACTOR, RERANK_CANDIDATES_MIN)
            if reranker is not None
            else top_k
        )
        hits = await self._index.search(tenant_id, query, dense, limit)
        if reranker is None or len(hits) <= 1:
            return hits[:top_k]
        return await _reranked(reranker, tenant_id, query, hits, top_k)


async def _reranked(
    reranker: Reranker, tenant_id: TenantId, query: str, hits: Sequence[ChunkHit], top_k: int
) -> list[ChunkHit]:
    try:
        order = await reranker.rerank(query, [hit.text for hit in hits], top_k)
    except RerankError as error:
        logger.warning("knowledge.rerank_failed", tenant_id=str(tenant_id), error=str(error))
        return list(hits[:top_k])
    picked: list[ChunkHit] = []
    seen: set[int] = set()
    for i in order:
        if 0 <= i < len(hits) and i not in seen:
            seen.add(i)
            picked.append(hits[i])
    if not picked:
        logger.warning("knowledge.rerank_empty", tenant_id=str(tenant_id), candidates=len(hits))
        return list(hits[:top_k])
    return picked[:top_k]
