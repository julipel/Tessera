"""Сборка поиска по знаниям из настроек — для API (`main.py`) и раннера эвалов: эмбеддер,
индекс и (если настроен, ADR-0015) реранкер.

Эмбеддер запроса и индекс — те же, что пишет воркер (`EMBEDDING_*`, `QDRANT_*`): иначе
векторы запроса и чанков несравнимы. Коллекцию создаёт воркер (`ensure_collection`);
её отсутствие при поиске — ошибка инструмента, а не старта API.
"""

from dataclasses import dataclass

import structlog

from app.modules.knowledge.public import (
    HttpReranker,
    KnowledgeSearch,
    OpenAIEmbedder,
    QdrantChunkIndex,
    create_http_reranker,
    create_openai_embedder,
    create_qdrant_index,
)
from app.settings import Settings

logger = structlog.get_logger(__name__)


@dataclass(frozen=True, slots=True)
class KnowledgeServices:
    search: KnowledgeSearch
    embedder: OpenAIEmbedder
    index: QdrantChunkIndex
    reranker: HttpReranker | None = None

    async def aclose(self) -> None:
        await self.embedder.aclose()
        await self.index.aclose()
        if self.reranker is not None:
            await self.reranker.aclose()


def build_knowledge_services(settings: Settings) -> KnowledgeServices | None:
    """None без OPENAI_API_KEY: запрос не во что эмбеддить, `search_knowledge` не подключится.
    Сетевых вызовов не делает — клиенты ленивые."""
    if settings.openai_api_key is None or not settings.openai_api_key.get_secret_value():
        logger.warning("knowledge.search_disabled", reason="OPENAI_API_KEY не задан")
        return None
    embedder = create_openai_embedder(
        settings.openai_api_key.get_secret_value(),
        model=settings.embedding_model,
        dimensions=settings.embedding_dimensions,
        base_url=settings.openai_base_url,
        batch_size=settings.embedding_batch_size,
        # Запрос — в ходе диалога: таймаут инструмента 10 с, долгие ретраи бессмысленны.
        timeout_s=5.0,
        max_retries=1,
    )
    qdrant_api_key = settings.qdrant_api_key.get_secret_value() if settings.qdrant_api_key else None
    index = create_qdrant_index(
        settings.qdrant_url,
        collection=settings.qdrant_collection,
        dimensions=settings.embedding_dimensions,
        api_key=qdrant_api_key or None,
        timeout_s=5.0,
    )
    reranker = build_reranker(settings)
    return KnowledgeServices(KnowledgeSearch(embedder, index, reranker), embedder, index, reranker)


def build_reranker(settings: Settings) -> HttpReranker | None:
    """Реранкер, если заданы RERANK_URL и RERANK_MODEL (ADR-0015); иначе None —
    `KnowledgeSearch` отдаёт порядок RRF."""
    url, model = settings.rerank_url or None, settings.rerank_model or None
    if url is None or model is None:
        if url or model:
            logger.warning(
                "knowledge.rerank_disabled", reason="задан только один из RERANK_URL и RERANK_MODEL"
            )
        return None
    api_key = settings.rerank_api_key.get_secret_value() if settings.rerank_api_key else None
    return create_http_reranker(
        url, model=model, api_key=api_key or None, timeout_s=settings.rerank_timeout_s
    )
