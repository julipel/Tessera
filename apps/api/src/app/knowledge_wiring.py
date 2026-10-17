"""Сборка knowledge из настроек. Поиск — для API (`main.py`) и раннера эвалов: эмбеддер,
индекс и (если настроен, ADR-0015) реранкер. Коннекторы и эмбеддер чанков — для воркера
и CLI (CLI не импортирует воркер, REVIEW.md: S-15).

Эмбеддер запроса и индекс — те же, что пишет воркер (`EMBEDDING_*`, `QDRANT_*`): иначе
векторы запроса и чанков несравнимы. Коллекцию создаёт воркер (`ensure_collection`);
её отсутствие при поиске — ошибка инструмента, а не старта API.
"""

from dataclasses import dataclass

import structlog

from app.lifecycle import close_all
from app.modules.knowledge.public import (
    DatabaseConnector,
    FileConnector,
    HttpApiConnector,
    HttpReranker,
    KnowledgeSearch,
    OpenAIEmbedder,
    QdrantChunkIndex,
    SourceConnector,
    SourceKind,
    SourceSecrets,
    TableConnector,
    WebClient,
    WebsiteConnector,
    create_http_reranker,
    create_openai_embedder,
    create_qdrant_index,
    parse_networks,
)
from app.settings import Settings, secret_value, source_secrets_env

logger = structlog.get_logger(__name__)


@dataclass(frozen=True, slots=True)
class KnowledgeServices:
    search: KnowledgeSearch
    embedder: OpenAIEmbedder
    index: QdrantChunkIndex
    reranker: HttpReranker | None = None

    async def aclose(self) -> None:
        reranker = [("reranker", self.reranker.aclose)] if self.reranker is not None else []
        await close_all(
            [("embedder", self.embedder.aclose), ("index", self.index.aclose), *reranker]
        )


def build_knowledge_services(settings: Settings) -> KnowledgeServices | None:
    """None без OPENAI_API_KEY: запрос не во что эмбеддить, `search_knowledge` не подключится.
    Сетевых вызовов не делает — клиенты ленивые."""
    openai_api_key = secret_value(settings.openai_api_key)
    if openai_api_key is None:
        logger.warning("knowledge.search_disabled", reason="OPENAI_API_KEY не задан")
        return None
    embedder = create_openai_embedder(
        openai_api_key,
        model=settings.embedding_model,
        dimensions=settings.embedding_dimensions,
        base_url=settings.openai_base_url,
        batch_size=settings.embedding_batch_size,
        # Запрос — в ходе диалога: таймаут инструмента 10 с; две попытки по 3 с в типичном
        # случае оставляют время на Qdrant и реранкер, общего дедлайна поиска нет (K-18).
        timeout_s=3.0,
        max_retries=1,
    )
    index = create_qdrant_index(
        settings.qdrant_url,
        collection=settings.qdrant_collection,
        dimensions=settings.embedding_dimensions,
        api_key=secret_value(settings.qdrant_api_key),
        timeout_s=3.0,
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
    return create_http_reranker(
        url,
        model=model,
        api_key=secret_value(settings.rerank_api_key),
        timeout_s=settings.rerank_timeout_s,
    )


def build_connectors(
    settings: Settings, web_client: WebClient
) -> dict[SourceKind, SourceConnector]:
    """Коннекторы источников по виду."""
    secrets = SourceSecrets(source_secrets_env())
    return {
        SourceKind.FILE: FileConnector(settings.knowledge_files_dir),
        SourceKind.TABLE: TableConnector(settings.knowledge_files_dir),
        SourceKind.WEBSITE: WebsiteConnector(web_client),
        SourceKind.HTTP_API: HttpApiConnector(web_client, secrets),
        SourceKind.DATABASE: DatabaseConnector(
            secrets, allowed_networks=parse_networks(settings.source_db_allowed_networks)
        ),
    }


def build_embedder(settings: Settings) -> OpenAIEmbedder:
    """Эмбеддер чанков; без OPENAI_API_KEY воркер не стартует — синхронизация без
    эмбеддингов бесполезна (каждый документ упал бы ошибкой элемента)."""
    api_key = secret_value(settings.openai_api_key)
    if not api_key:
        raise RuntimeError("OPENAI_API_KEY не задан: эмбеддинги чанков знаний недоступны")
    return create_openai_embedder(
        api_key,
        model=settings.embedding_model,
        dimensions=settings.embedding_dimensions,
        base_url=settings.openai_base_url,
        batch_size=settings.embedding_batch_size,
    )
