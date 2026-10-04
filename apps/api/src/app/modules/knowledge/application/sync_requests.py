"""Запрос синхронизации источника: запись SourceSync + постановка в очередь воркера.

Параллельный запуск одного источника исключён: активная синхронизация (pending/running)
у источника одна — повторный запрос возвращает её и ставит в очередь ещё раз. Дубля
задачи не будет (очередь дедуплицирует по sync_id), а потерянная задача (упал воркер,
очищен Redis) так поднимается заново.
"""

from uuid import UUID

import structlog

from app.modules.knowledge.domain.ports import SyncQueue, SyncStore
from app.modules.shared.kernel import NotFoundError, TenantId

logger = structlog.get_logger(__name__)


async def request_sync(
    tenant_id: TenantId,
    source_id: UUID,
    store: SyncStore,
    queue: SyncQueue,
    *,
    full: bool = False,
) -> UUID:
    """Вернуть id синхронизации, поставленной в очередь.

    `full` действует только для новой синхронизации: у уже активной режим не меняется.
    """
    sync_id = await store.open_sync(tenant_id, source_id)
    if sync_id is None:
        raise NotFoundError(f"Source {source_id} not found")
    # Коммит до постановки: воркер не должен увидеть sync_id раньше записи в БД.
    await store.commit()
    await queue.enqueue(tenant_id, sync_id, full=full)
    logger.info(
        "ingestion.sync_requested",
        tenant_id=str(tenant_id),
        source_id=str(source_id),
        sync_id=str(sync_id),
        full=full,
    )
    return sync_id
