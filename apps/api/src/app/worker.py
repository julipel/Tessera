"""arq-воркер фоновых задач: `make worker` (arq app.worker.WorkerSettings).

Composition root воркера, как `main.py` для API: ресурсы создаются в `startup`
и лежат в ctx задачи. Задачи ставятся через `ArqSyncQueue` (modules/knowledge);
чистка по срокам хранения (ADR-0043) — cron воркера раз в сутки.
"""

import uuid
from collections.abc import Awaitable, Callable, Mapping
from datetime import UTC, datetime
from typing import Any, ClassVar, cast
from uuid import UUID

import structlog
from arq.connections import RedisSettings
from arq.cron import CronJob, cron
from pydantic import SecretStr
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.logs import configure_logging
from app.modules.knowledge.public import (
    Chunker,
    ChunkIndex,
    DatabaseConnector,
    Embedder,
    FileConnector,
    HttpApiConnector,
    MarkdownChunker,
    OpenAIEmbedder,
    SourceConnector,
    SourceKind,
    SourceSecrets,
    SqlSyncStore,
    TableConnector,
    WebClient,
    WebsiteConnector,
    build_web_client,
    create_openai_embedder,
    create_qdrant_index,
    parse_networks,
    run_sync,
)
from app.modules.shared.public import TenantId, create_engine, create_session_factory
from app.retention import purge_all_tenants
from app.settings import Settings, source_secrets_env

logger = structlog.get_logger(__name__)

# На запись failed после дедлайна синхронизации, до того как задачу оборвёт arq.
SYNC_FINISH_MARGIN_S = 120
# Чистка по срокам хранения: порции с commit, прерванная продолжится на следующий день.
RETENTION_TIMEOUT_S = 3600


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
    api_key = _secret(settings.openai_api_key)
    if not api_key:
        raise RuntimeError("OPENAI_API_KEY не задан: эмбеддинги чанков знаний недоступны")
    return create_openai_embedder(
        api_key,
        model=settings.embedding_model,
        dimensions=settings.embedding_dimensions,
        base_url=settings.openai_base_url,
        batch_size=settings.embedding_batch_size,
    )


def _secret(value: SecretStr | None) -> str | None:
    return value.get_secret_value() if value is not None else None


async def sync_source(ctx: dict[str, Any], tenant_id: str, sync_id: str, full: bool) -> None:
    """Задача воркера: выполнить синхронизацию `sync_id` (см. knowledge `run_sync`).

    Ошибки синхронизации `run_sync` пишет в SourceSync сам; наружу (в arq) выходят только
    сбои инфраструктуры — БД недоступна и т. п.
    """
    session_factory = cast(async_sessionmaker[AsyncSession], ctx["session_factory"])
    connectors = cast(Mapping[SourceKind, SourceConnector], ctx["connectors"])
    chunker = cast(Chunker, ctx["chunker"])
    embedder = cast(Embedder, ctx["embedder"])
    index = cast(ChunkIndex, ctx["index"])
    trace_id = str(ctx.get("job_id") or uuid.uuid4())
    with structlog.contextvars.bound_contextvars(
        tenant_id=tenant_id, sync_id=sync_id, trace_id=trace_id
    ):
        async with session_factory() as session:
            await run_sync(
                TenantId(UUID(tenant_id)),
                UUID(sync_id),
                SqlSyncStore(session),
                connectors,
                chunker,
                embedder=embedder,
                index=index,
                full=full,
                timeout_s=cast(float, ctx["sync_timeout_s"]),
            )


async def purge_expired_data(ctx: dict[str, Any]) -> None:
    """Ежедневная чистка данных посетителей по срокам хранения тенантов (ADR-0043)."""
    session_factory = cast(async_sessionmaker[AsyncSession], ctx["session_factory"])
    trace_id = str(ctx.get("job_id") or uuid.uuid4())
    with structlog.contextvars.bound_contextvars(trace_id=trace_id):
        run = await purge_all_tenants(session_factory, datetime.now(UTC))
    if not run.ok:
        # Остальное уже почищено; задача — failed, чтобы сбой был виден в arq.
        parts = [f"тенанты {', '.join(run.failed)}"] if run.failed else []
        parts += ["сессии админки"] if run.admin_sessions_failed else []
        raise RuntimeError(f"retention: сбой: {'; '.join(parts)}")


async def startup(ctx: dict[str, Any]) -> None:
    settings = Settings()
    configure_logging(settings)
    engine = create_engine(
        settings.database_url,
        echo=settings.database_echo,
        statement_timeout_s=settings.db_statement_timeout_s,
    )
    ctx["engine"] = engine
    ctx["sync_timeout_s"] = settings.sync_timeout_s
    ctx["session_factory"] = create_session_factory(engine)
    web_client = build_web_client(
        user_agent=settings.crawler_user_agent, timeout_s=settings.crawler_timeout_s
    )
    ctx["web_client"] = web_client
    ctx["connectors"] = build_connectors(settings, web_client)
    ctx["chunker"] = MarkdownChunker()
    ctx["embedder"] = build_embedder(settings)
    index = create_qdrant_index(
        settings.qdrant_url,
        collection=settings.qdrant_collection,
        dimensions=settings.embedding_dimensions,
        api_key=_secret(settings.qdrant_api_key),
    )
    ctx["index"] = index
    await index.ensure_collection()
    logger.info("worker.started", qdrant_collection=settings.qdrant_collection)


async def shutdown(ctx: dict[str, Any]) -> None:
    await ctx["web_client"].http.aclose()
    await ctx["embedder"].aclose()
    await ctx["index"].aclose()
    await ctx["engine"].dispose()


class WorkerSettings:
    functions: ClassVar[list[Callable[..., Awaitable[None]]]] = [sync_source]
    # Раз в сутки ночью по UTC; unique — одна чистка при нескольких воркерах. Таймаут — свой:
    # первая чистка накопленного может идти дольше job_timeout синхронизаций.
    cron_jobs: ClassVar[list[CronJob]] = [
        cron(purge_expired_data, hour=3, minute=17, timeout=RETENTION_TIMEOUT_S)
    ]
    on_startup = startup
    on_shutdown = shutdown
    _settings = Settings()
    redis_settings = RedisSettings.from_dsn(_settings.redis_url)
    # Таймаут arq по умолчанию (300 с) обрывал бы длинные синхронизации, оставляя их в running.
    job_timeout = _settings.sync_timeout_s + SYNC_FINISH_MARGIN_S
