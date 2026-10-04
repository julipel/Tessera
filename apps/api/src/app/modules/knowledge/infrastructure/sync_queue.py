"""SyncQueue поверх arq (порт из domain/ports.py)."""

from typing import Any, Protocol
from uuid import UUID

from app.modules.shared.public import TenantId

# Имя задачи воркера (app/worker.py регистрирует функцию с этим именем).
SYNC_SOURCE_JOB = "sync_source"


def sync_job_id(sync_id: UUID) -> str:
    return f"sync:{sync_id}"


class JobEnqueuer(Protocol):
    """Часть `arq.ArqRedis`, нужная очереди (подменяется в тестах)."""

    async def enqueue_job(self, function: str, *args: Any, **kwargs: Any) -> Any: ...


class ArqSyncQueue:
    def __init__(self, redis: JobEnqueuer) -> None:
        self.redis = redis

    async def enqueue(self, tenant_id: TenantId, sync_id: UUID, *, full: bool) -> None:
        # Фиксированный _job_id: arq отклоняет задачу, пока такая же в очереди,
        # выполняется или хранится её результат, — одна синхронизация не идёт дважды.
        await self.redis.enqueue_job(
            SYNC_SOURCE_JOB, str(tenant_id), str(sync_id), full, _job_id=sync_job_id(sync_id)
        )
