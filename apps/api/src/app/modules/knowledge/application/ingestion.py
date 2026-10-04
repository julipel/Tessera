"""Пайплайн синхронизации источника: коннектор → нормализованные элементы → Postgres.

Статусы `SourceSync`: pending → running → succeeded | failed. Каждый элемент коммитится
отдельно: сбой одного элемента не откатывает остальные и не валит синхронизацию.
Элемент с тем же content_hash не перезаписывается (дедупликация).
"""

from collections.abc import Mapping
from uuid import UUID

import structlog

from app.modules.knowledge.domain.entities import SourceKind, SyncStatus
from app.modules.knowledge.domain.errors import NoConnectorError
from app.modules.knowledge.domain.ingestion import (
    DocumentItem,
    ItemKey,
    SourceSpec,
    SyncJob,
    SyncStats,
    content_hash,
    item_key,
)
from app.modules.knowledge.domain.ports import Chunker, SourceConnector, SyncStore
from app.modules.shared.kernel import NotFoundError, TenantId

logger = structlog.get_logger(__name__)

_FINISHED = (SyncStatus.SUCCEEDED, SyncStatus.FAILED)


async def run_sync(
    tenant_id: TenantId,
    sync_id: UUID,
    store: SyncStore,
    connectors: Mapping[SourceKind, SourceConnector],
    chunker: Chunker,
    *,
    full: bool = False,
) -> None:
    """Выполнить синхронизацию `sync_id`. Без `full` при наличии курсора последней успешной
    синхронизации — инкрементально (changed_since), иначе полный discover с удалением
    пропавших элементов.

    Повторный запуск завершённой синхронизации ничего не делает; зависшую в running
    (воркер упал) — выполняет заново: запись идемпотентна.
    """
    job = await store.load_job(tenant_id, sync_id)
    if job is None:
        raise NotFoundError(f"SourceSync {sync_id} not found")
    log = logger.bind(tenant_id=str(tenant_id), source_id=str(job.source_id), sync_id=str(sync_id))
    if job.status in _FINISHED:
        log.info("ingestion.sync_already_finished", status=job.status)
        return

    await store.mark_running(tenant_id, sync_id)
    await store.commit()
    log.info("ingestion.sync_started", kind=job.kind)

    stats = SyncStats()
    try:
        cursor = await _ingest(tenant_id, job, store, connectors, chunker, stats, full=full)
    except Exception as e:
        await store.rollback()
        await store.finish(
            tenant_id, sync_id, SyncStatus.FAILED, stats.as_dict(), None, f"{type(e).__name__}: {e}"
        )
        await store.commit()
        log.exception("ingestion.sync_failed", **stats.as_dict())
        return

    await store.finish(tenant_id, sync_id, SyncStatus.SUCCEEDED, stats.as_dict(), cursor, None)
    await store.commit()
    log.info("ingestion.sync_succeeded", **stats.as_dict())


async def _ingest(
    tenant_id: TenantId,
    job: SyncJob,
    store: SyncStore,
    connectors: Mapping[SourceKind, SourceConnector],
    chunker: Chunker,
    stats: SyncStats,
    *,
    full: bool,
) -> str | None:
    """Обработать элементы источника; вернуть курсор для следующей синхронизации."""
    connector = connectors.get(job.kind)
    if connector is None:
        raise NoConnectorError(f"нет коннектора для источника вида {job.kind}")

    source = SourceSpec(tenant_id, job.source_id, job.config)
    previous = None if full else await store.last_cursor(tenant_id, job.source_id)
    if previous is not None:
        stats.incremental = True
        listing = await connector.changed_since(source, previous)
    else:
        listing = await connector.discover(source)
    stats.discovered = len(listing.refs)

    known = await store.item_hashes(tenant_id, job.source_id)
    seen: set[ItemKey] = set()
    failed_ids: set[str] = set()
    for ref in listing.refs:
        try:
            item = await connector.fetch(source, ref)
            key = item_key(item)
            seen.add(key)
            digest = content_hash(item)
            if known.get(key) == digest:
                stats.unchanged += 1
                continue
            if isinstance(item, DocumentItem):
                chunks = chunker.chunk(item.text)
                await store.save_document(tenant_id, job.source_id, item, digest, chunks)
            else:
                await store.save_entity(tenant_id, job.source_id, item, digest)
            await store.commit()
        except Exception as e:
            await store.rollback()
            failed_ids.add(ref.external_id)
            stats.add_error(ref.external_id, e)
            continue
        if key in known:
            stats.updated += 1
        else:
            stats.created += 1

    if not stats.incremental:
        # Элемент, который не удалось получить, не удаляем: его вид (документ/сущность)
        # неизвестен, а сам он в источнике есть.
        stale = [k for k in known if k not in seen and k.external_id not in failed_ids]
        if stale:
            stats.deleted = await store.delete_items(tenant_id, job.source_id, stale)
            await store.commit()

    return listing.cursor if listing.cursor is not None else previous
