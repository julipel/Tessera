"""SourceAdminStore поверх Postgres (порт из domain/ports.py)."""

from collections.abc import Sequence
from typing import Any
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.knowledge.domain.entities import SourceKind, SourceOrigin
from app.modules.knowledge.domain.source_admin import SourceOverview, SyncRun
from app.modules.knowledge.infrastructure.models import (
    DocumentRecord,
    EntityRecord,
    SourceRecord,
    SourceSyncRecord,
)
from app.modules.knowledge.infrastructure.repositories import (
    DocumentRepository,
    EntityRepository,
    SourceRepository,
    SourceSyncRepository,
)
from app.modules.shared.public import TenantId


class SqlSourceAdminStore:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session
        self.sources = SourceRepository(session)
        self.syncs = SourceSyncRepository(session)
        self.documents = DocumentRepository(session)
        self.entities = EntityRepository(session)

    async def list_sources(self, tenant_id: TenantId) -> list[SourceOverview]:
        records = await self.sources.list(tenant_id)
        overviews = await self._overviews(tenant_id, records)
        return sorted(overviews, key=lambda s: (s.name is None, s.name or "", s.created_at))

    async def get_source(self, tenant_id: TenantId, source_id: UUID) -> SourceOverview | None:
        record = await self.sources.get(tenant_id, source_id)
        if record is None:
            return None
        return (await self._overviews(tenant_id, [record]))[0]

    async def create_source(
        self, tenant_id: TenantId, name: str, kind: SourceKind, config: dict[str, Any]
    ) -> UUID | None:
        return await self.sources.create_unique(tenant_id, name, kind, config, SourceOrigin.ADMIN)

    async def recent_syncs(self, tenant_id: TenantId, source_id: UUID, limit: int) -> list[SyncRun]:
        records = await self.syncs.recent(tenant_id, [source_id], per_source=limit)
        return [_sync_run(r) for r in records]

    async def get_sync(self, tenant_id: TenantId, sync_id: UUID) -> SyncRun | None:
        record = await self.syncs.get(tenant_id, sync_id)
        return None if record is None else _sync_run(record)

    async def commit(self) -> None:
        await self.session.commit()

    async def _overviews(
        self, tenant_id: TenantId, records: Sequence[SourceRecord]
    ) -> list[SourceOverview]:
        ids = [r.id for r in records]
        if not ids:
            return []
        # Документы-описания сущностей считаются в сущностях, а не в документах.
        documents = await self.documents.counts(
            tenant_id, DocumentRecord.source_id.in_(ids), DocumentRecord.entity_id.is_(None)
        )
        entities = await self.entities.counts(tenant_id, EntityRecord.source_id.in_(ids))
        last: dict[UUID, SourceSyncRecord] = {}
        for sync in await self.syncs.recent(tenant_id, ids, per_source=1):
            last[sync.source_id] = sync
        return [
            SourceOverview(
                id=r.id,
                name=r.name,
                kind=r.kind,
                origin=r.origin,
                status=r.status,
                created_at=r.created_at,
                config=r.config,
                documents=documents.get(r.id, 0),
                entities=entities.get(r.id, 0),
                last_sync=_sync_run(last[r.id]) if r.id in last else None,
            )
            for r in records
        ]


def _sync_run(record: SourceSyncRecord) -> SyncRun:
    return SyncRun(
        id=record.id,
        status=record.status,
        created_at=record.created_at,
        started_at=record.started_at,
        finished_at=record.finished_at,
        # stats пишет только завершение синхронизации (`SyncStore.finish`).
        stats=record.stats if record.finished_at is not None else None,
        error=record.error,
    )
