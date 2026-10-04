"""Фоновая синхронизация: запрос в очередь, задача arq-воркера, одна активная на источник."""

from dataclasses import dataclass, field
from typing import Any
from uuid import UUID, uuid4

import pytest
from pydantic import SecretStr
from sqlalchemy import exc
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncSession, async_sessionmaker

from app.modules.knowledge.public import (
    SYNC_SOURCE_JOB,
    ArqSyncQueue,
    DocumentItem,
    DocumentRepository,
    Listing,
    MarkdownChunker,
    RawItem,
    RawItemRef,
    SourceKind,
    SourceRecord,
    SourceRepository,
    SourceSpec,
    SourceSyncRecord,
    SourceSyncRepository,
    SqlSyncStore,
    SyncQueue,
    SyncStatus,
    request_sync,
)
from app.modules.shared.public import NotFoundError, TenantId
from app.modules.tenants.public import SqlTenantDirectory
from app.settings import Settings
from app.worker import WorkerSettings, build_embedder, sync_source
from knowledge_fakes import FakeEmbedder, InMemoryChunkIndex


@dataclass
class OneDocConnector:
    kind: SourceKind = SourceKind.FILE

    async def discover(self, source: SourceSpec) -> Listing:
        return Listing([RawItemRef("faq")], None)

    async def fetch(self, source: SourceSpec, ref: RawItemRef) -> RawItem:
        return DocumentItem(external_id=ref.external_id, title="FAQ", text="Доставка 2 дня.")

    async def changed_since(self, source: SourceSpec, cursor: str) -> Listing:
        return Listing([], cursor)


@dataclass
class FakeSyncQueue:
    jobs: list[tuple[TenantId, UUID, bool]] = field(default_factory=list)

    async def enqueue(self, tenant_id: TenantId, sync_id: UUID, *, full: bool) -> None:
        self.jobs.append((tenant_id, sync_id, full))


@dataclass
class FakeRedis:
    calls: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = field(default_factory=list)

    async def enqueue_job(self, function: str, *args: Any, **kwargs: Any) -> Any:
        self.calls.append((function, args, kwargs))
        return None


async def _source(session: AsyncSession, slug: str = "tenant-a") -> tuple[TenantId, UUID]:
    tenant_id = (await SqlTenantDirectory(session).create(slug, slug)).id
    source = await SourceRepository(session).add(
        tenant_id, SourceRecord(tenant_id=tenant_id, kind=SourceKind.FILE, config={})
    )
    return tenant_id, source.id


async def _status(session: AsyncSession, tenant_id: TenantId, sync_id: UUID) -> SyncStatus:
    record = await SourceSyncRepository(session).get_or_raise(tenant_id, sync_id)
    await session.refresh(record)
    return record.status


async def test_worker_task_runs_sync(
    db_connection: AsyncConnection, db_session: AsyncSession
) -> None:
    tenant_id, source_id = await _source(db_session)
    queue = FakeSyncQueue()
    sync_id = await request_sync(tenant_id, source_id, SqlSyncStore(db_session), queue)
    [(_, queued_id, full)] = queue.jobs
    index = InMemoryChunkIndex()

    ctx: dict[str, Any] = {
        "job_id": "sync:test",
        "session_factory": async_sessionmaker(
            bind=db_connection, join_transaction_mode="create_savepoint", expire_on_commit=False
        ),
        "connectors": {SourceKind.FILE: OneDocConnector()},
        "chunker": MarkdownChunker(),
        "embedder": FakeEmbedder(),
        "index": index,
    }
    await sync_source(ctx, str(tenant_id), str(queued_id), full)

    assert queued_id == sync_id
    assert await _status(db_session, tenant_id, sync_id) == SyncStatus.SUCCEEDED
    [document] = await DocumentRepository(db_session).list(tenant_id)
    assert document.external_id == "faq"
    assert index.external_ids(tenant_id) == {"faq"}


async def test_request_sync_creates_pending_and_enqueues(db_session: AsyncSession) -> None:
    tenant_id, source_id = await _source(db_session)
    queue = FakeSyncQueue()

    sync_id = await request_sync(tenant_id, source_id, SqlSyncStore(db_session), queue, full=True)

    assert await _status(db_session, tenant_id, sync_id) == SyncStatus.PENDING
    assert queue.jobs == [(tenant_id, sync_id, True)]


@pytest.mark.parametrize("active", [SyncStatus.PENDING, SyncStatus.RUNNING])
async def test_active_sync_is_reused_and_requeued(
    db_session: AsyncSession, active: SyncStatus
) -> None:
    tenant_id, source_id = await _source(db_session)
    store, queue = SqlSyncStore(db_session), FakeSyncQueue()
    first = await request_sync(tenant_id, source_id, store, queue)
    await SourceSyncRepository(db_session).set_fields(tenant_id, first, status=active)

    second = await request_sync(tenant_id, source_id, store, queue)

    assert second == first
    assert [job[1] for job in queue.jobs] == [first, first]
    assert len(await SourceSyncRepository(db_session).list(tenant_id)) == 1


@pytest.mark.parametrize("finished", [SyncStatus.SUCCEEDED, SyncStatus.FAILED])
async def test_new_sync_after_finished(db_session: AsyncSession, finished: SyncStatus) -> None:
    tenant_id, source_id = await _source(db_session)
    store, queue = SqlSyncStore(db_session), FakeSyncQueue()
    first = await request_sync(tenant_id, source_id, store, queue)
    await SourceSyncRepository(db_session).set_fields(tenant_id, first, status=finished)

    second = await request_sync(tenant_id, source_id, store, queue)

    assert second != first
    assert await _status(db_session, tenant_id, second) == SyncStatus.PENDING


async def test_database_forbids_two_active_syncs_of_one_source(db_session: AsyncSession) -> None:
    tenant_id, source_id = await _source(db_session)
    repo = SourceSyncRepository(db_session)
    await repo.add(tenant_id, SourceSyncRecord(tenant_id=tenant_id, source_id=source_id))

    with pytest.raises(exc.IntegrityError):
        async with db_session.begin_nested():
            await repo.add(
                tenant_id,
                SourceSyncRecord(
                    tenant_id=tenant_id, source_id=source_id, status=SyncStatus.RUNNING
                ),
            )


async def test_syncs_of_different_sources_do_not_block_each_other(
    db_session: AsyncSession,
) -> None:
    tenant_id, source_id = await _source(db_session)
    other = await SourceRepository(db_session).add(
        tenant_id, SourceRecord(tenant_id=tenant_id, kind=SourceKind.FILE, config={})
    )
    store, queue = SqlSyncStore(db_session), FakeSyncQueue()

    first = await request_sync(tenant_id, source_id, store, queue)
    second = await request_sync(tenant_id, other.id, store, queue)

    assert first != second


async def test_request_sync_of_other_tenant_source_is_not_found(db_session: AsyncSession) -> None:
    _, source_id = await _source(db_session, "tenant-a")
    tenant_b, _ = await _source(db_session, "tenant-b")
    queue = FakeSyncQueue()

    with pytest.raises(NotFoundError):
        await request_sync(tenant_b, source_id, SqlSyncStore(db_session), queue)
    with pytest.raises(NotFoundError):
        await request_sync(tenant_b, uuid4(), SqlSyncStore(db_session), queue)

    assert queue.jobs == []
    assert await SourceSyncRepository(db_session).list(tenant_b) == []


async def test_arq_queue_enqueues_worker_job_with_unique_id() -> None:
    redis = FakeRedis()
    tenant_id, sync_id = TenantId(uuid4()), uuid4()

    await ArqSyncQueue(redis).enqueue(tenant_id, sync_id, full=True)

    assert redis.calls == [
        (SYNC_SOURCE_JOB, (str(tenant_id), str(sync_id), True), {"_job_id": f"sync:{sync_id}"})
    ]


def test_worker_registers_sync_job_under_queue_name() -> None:
    assert [f.__name__ for f in WorkerSettings.functions] == [SYNC_SOURCE_JOB]


def test_fakes_satisfy_protocols() -> None:
    _queue: SyncQueue = FakeSyncQueue()
    _redis = ArqSyncQueue(FakeRedis())


def test_worker_requires_openai_key_for_embeddings() -> None:
    with pytest.raises(RuntimeError, match="OPENAI_API_KEY"):
        build_embedder(Settings(_env_file=None, openai_api_key=None))


def test_worker_builds_embedder_from_settings() -> None:
    embedder = build_embedder(
        Settings(
            _env_file=None,
            openai_api_key=SecretStr("sk-test"),
            embedding_model="emb-test",
            embedding_dimensions=8,
            embedding_batch_size=16,
        )
    )
    assert (embedder.model, embedder.dimensions, embedder.batch_size) == ("emb-test", 8, 16)
