"""API админки: источники знаний (docs/contracts.md §7, ADR-0037). Чтение — роль viewer,
создание и запуск синхронизации — editor."""

from uuid import UUID

from fastapi import APIRouter, Request, status

from app.contracts import (
    CreateSourceRequest,
    ErrorDetail,
    SourceDetail,
    SourceList,
    SourceSummary,
    SourceSyncItem,
    SourceSyncList,
    SourceSyncStats,
    StartSyncRequest,
)
from app.modules.knowledge.application.source_admin import (
    create_source,
    get_source,
    list_sources,
    list_syncs,
    source_config_yaml,
    start_sync,
)
from app.modules.knowledge.domain.entities import SourceKind
from app.modules.knowledge.domain.errors import InvalidSourceError
from app.modules.knowledge.domain.ports import SyncQueue
from app.modules.knowledge.domain.source_admin import SourceOverview, SyncRun
from app.modules.knowledge.infrastructure.source_admin_store import SqlSourceAdminStore
from app.modules.knowledge.infrastructure.source_configs import validate_source_config
from app.modules.knowledge.infrastructure.sync_store import SqlSyncStore
from app.modules.shared.public import (
    AdminEditor,
    AdminViewer,
    ApiError,
    DbSession,
    NotFoundError,
    TenantId,
)

router = APIRouter(prefix="/v1/admin/tenants/{tenant_id}/sources", tags=["admin"])


@router.get("", dependencies=[AdminViewer])
async def sources(tenant_id: UUID, session: DbSession) -> SourceList:
    overviews = await list_sources(TenantId(tenant_id), SqlSourceAdminStore(session))
    return SourceList(sources=[_summary(s) for s in overviews])


@router.get("/{source_id}", dependencies=[AdminViewer])
async def source(tenant_id: UUID, source_id: UUID, session: DbSession) -> SourceDetail:
    try:
        overview = await get_source(TenantId(tenant_id), source_id, SqlSourceAdminStore(session))
    except NotFoundError as e:
        raise _not_found() from e
    return _detail(overview)


@router.post("", status_code=status.HTTP_201_CREATED, dependencies=[AdminEditor])
async def new_source(
    tenant_id: UUID, body: CreateSourceRequest, session: DbSession
) -> SourceDetail:
    """Источник website, file или table; 422 `invalid_input` с `details` — ошибки по местам
    (`name`, `kind`, `config_yaml` и путь внутри конфига)."""
    try:
        overview = await create_source(
            TenantId(tenant_id),
            body.name,
            SourceKind(body.kind),
            body.config_yaml,
            SqlSourceAdminStore(session),
            validate_source_config,
        )
    except InvalidSourceError as e:
        raise ApiError(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            "invalid_input",
            str(e),
            details=[ErrorDetail(loc=list(p.loc), message=p.message) for p in e.problems],
        ) from e
    return _detail(overview)


@router.get("/{source_id}/syncs", dependencies=[AdminViewer])
async def source_syncs(tenant_id: UUID, source_id: UUID, session: DbSession) -> SourceSyncList:
    try:
        runs = await list_syncs(TenantId(tenant_id), source_id, SqlSourceAdminStore(session))
    except NotFoundError as e:
        raise _not_found() from e
    return SourceSyncList(syncs=[_sync(r) for r in runs])


@router.post("/{source_id}/sync", status_code=status.HTTP_202_ACCEPTED, dependencies=[AdminEditor])
async def sync_source(
    request: Request,
    tenant_id: UUID,
    source_id: UUID,
    session: DbSession,
    body: StartSyncRequest | None = None,
) -> SourceSyncItem:
    """Поставить синхронизацию в очередь воркера; если она уже ждёт или идёт — вернуть её."""
    queue: SyncQueue = request.app.state.sync_queue
    try:
        run = await start_sync(
            TenantId(tenant_id),
            source_id,
            SqlSourceAdminStore(session),
            SqlSyncStore(session),
            queue,
            full=bool(body and body.full),
        )
    except NotFoundError as e:
        raise _not_found() from e
    return _sync(run)


def _summary(source: SourceOverview) -> SourceSummary:
    return SourceSummary(
        id=source.id,
        name=source.name,
        kind=source.kind,
        origin=source.origin,
        status=source.status,
        created_at=source.created_at,
        documents=source.documents,
        entities=source.entities,
        last_sync=_sync(source.last_sync) if source.last_sync else None,
    )


def _detail(source: SourceOverview) -> SourceDetail:
    return SourceDetail(
        **_summary(source).model_dump(), config_yaml=source_config_yaml(source.config)
    )


def _sync(run: SyncRun) -> SourceSyncItem:
    return SourceSyncItem(
        id=run.id,
        status=run.status,
        created_at=run.created_at,
        started_at=run.started_at,
        finished_at=run.finished_at,
        stats=SourceSyncStats.model_validate(run.stats) if run.stats is not None else None,
        error=run.error,
    )


def _not_found() -> ApiError:
    return ApiError(status.HTTP_404_NOT_FOUND, "not_found", "источник не найден")
