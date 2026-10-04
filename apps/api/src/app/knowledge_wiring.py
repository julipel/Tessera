"""Сборка поиска по знаниям из настроек — для API (`main.py`) и раннера эвалов.

Эмбеддер запроса и индекс — те же, что пишет воркер (`EMBEDDING_*`, `QDRANT_*`): иначе
векторы запроса и чанков несравнимы. Коллекцию создаёт воркер (`ensure_collection`);
её отсутствие при поиске — ошибка инструмента, а не старта API.
"""

from dataclasses import dataclass

import structlog

from app.modules.knowledge.public import (
    KnowledgeSearch,
    OpenAIEmbedder,
    QdrantChunkIndex,
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

    async def aclose(self) -> None:
        await self.embedder.aclose()
        await self.index.aclose()


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
    return KnowledgeServices(KnowledgeSearch(embedder, index), embedder, index)
