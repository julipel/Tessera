"""arq-воркер фоновых задач: `make worker` (arq app.worker.WorkerSettings).

Composition root воркера, как `main.py` для API: ресурсы создаются в `startup`
и лежат в ctx задачи. Задачи ставятся через `ArqSyncQueue` (modules/knowledge).
"""

import uuid
from collections.abc import Awaitable, Callable, Mapping
from typing import Any, ClassVar, cast
from uuid import UUID

import structlog
from arq.connections import RedisSettings
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.logs import configure_logging
from app.modules.knowledge.public import (
    Chunker,
    FileConnector,
    MarkdownChunker,
    SourceConnector,
    SourceKind,
    SqlSyncStore,
    run_sync,
)
from app.modules.shared.public import TenantId, create_engine, create_session_factory
from app.settings import Settings

logger = structlog.get_logger(__name__)


def build_connectors(settings: Settings) -> dict[SourceKind, SourceConnector]:
    """Коннекторы источников по виду."""
    return {SourceKind.FILE: FileConnector(settings.knowledge_files_dir)}


async def sync_source(ctx: dict[str, Any], tenant_id: str, sync_id: str, full: bool) -> None:
    """Задача воркера: выполнить синхронизацию `sync_id` (см. knowledge `run_sync`).

    Ошибки синхронизации `run_sync` пишет в SourceSync сам; наружу (в arq) выходят только
    сбои инфраструктуры — БД недоступна и т. п.
    """
    session_factory = cast(async_sessionmaker[AsyncSession], ctx["session_factory"])
    connectors = cast(Mapping[SourceKind, SourceConnector], ctx["connectors"])
    chunker = cast(Chunker, ctx["chunker"])
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
                full=full,
            )


async def startup(ctx: dict[str, Any]) -> None:
    settings = Settings()
    configure_logging(settings)
    engine = create_engine(settings.database_url, echo=settings.database_echo)
    ctx["engine"] = engine
    ctx["session_factory"] = create_session_factory(engine)
    ctx["connectors"] = build_connectors(settings)
    ctx["chunker"] = MarkdownChunker()
    logger.info("worker.started")


async def shutdown(ctx: dict[str, Any]) -> None:
    await ctx["engine"].dispose()


class WorkerSettings:
    functions: ClassVar[list[Callable[..., Awaitable[None]]]] = [sync_source]
    on_startup = startup
    on_shutdown = shutdown
    redis_settings = RedisSettings.from_dsn(Settings().redis_url)
