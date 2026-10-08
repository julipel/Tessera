"""API админки: источники знаний (docs/contracts.md §7, ADR-0037). Чтение — роль viewer,
создание, запуск синхронизации и файлы — editor."""

from uuid import UUID

from fastapi import APIRouter, Request, Response, status

from app.contracts import (
    CreateSourceRequest,
    ErrorDetail,
    SourceDetail,
    SourceFile,
    SourceFileList,
    SourceList,
    SourceSummary,
    SourceSyncItem,
    SourceSyncList,
    SourceSyncStats,
    StartSyncRequest,
)
from app.modules.knowledge.application.source_admin import (
    create_source,
    delete_source_file,
    get_source,
    list_source_files,
    list_sources,
    list_syncs,
    source_config_yaml,
    start_sync,
    upload_source_file,
)
from app.modules.knowledge.domain.entities import SourceKind
from app.modules.knowledge.domain.errors import InvalidSourceError, SourceFileTooLargeError
from app.modules.knowledge.domain.ports import SourceFileStore, SyncQueue
from app.modules.knowledge.domain.source_admin import (
    SourceFileInfo,
    SourceFileLimits,
    SourceOverview,
    SyncRun,
)
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
        raise _invalid(e, with_details=True) from e
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


@router.get("/{source_id}/files", dependencies=[AdminViewer])
async def source_files(
    request: Request, tenant_id: UUID, source_id: UUID, session: DbSession
) -> SourceFileList:
    """Файлы источника file/table (и из YAML тенанта — только посмотреть)."""
    try:
        files = await list_source_files(
            TenantId(tenant_id), source_id, SqlSourceAdminStore(session), _files(request)
        )
    except NotFoundError as e:
        raise _not_found() from e
    except InvalidSourceError as e:
        raise _invalid(e) from e
    return SourceFileList(files=[_file(f) for f in files])


@router.post("/{source_id}/files/{filename}", dependencies=[AdminEditor])
async def upload_file(
    request: Request, tenant_id: UUID, source_id: UUID, filename: str, session: DbSession
) -> SourceFile:
    """Тело запроса — байты файла (без multipart). Файл с тем же именем заменяется;
    синхронизацию загрузка не запускает. Больше лимита — 413."""
    limits: SourceFileLimits = request.app.state.source_file_limits
    try:
        data = await _read_body(request, limits.max_bytes)
        info = await upload_source_file(
            TenantId(tenant_id),
            source_id,
            filename,
            data,
            SqlSourceAdminStore(session),
            _files(request),
            limits,
        )
    except NotFoundError as e:
        raise _not_found() from e
    except InvalidSourceError as e:
        raise _invalid(e) from e
    except SourceFileTooLargeError as e:
        raise ApiError(status.HTTP_413_CONTENT_TOO_LARGE, "invalid_input", str(e)) from e
    return _file(info)


@router.delete(
    "/{source_id}/files/{filename}",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[AdminEditor],
)
async def delete_file(
    request: Request, tenant_id: UUID, source_id: UUID, filename: str, session: DbSession
) -> Response:
    """Документы и сущности файла удалит следующая полная синхронизация."""
    try:
        await delete_source_file(
            TenantId(tenant_id), source_id, filename, SqlSourceAdminStore(session), _files(request)
        )
    except NotFoundError as e:
        raise ApiError(status.HTTP_404_NOT_FOUND, "not_found", str(e)) from e
    except InvalidSourceError as e:
        raise _invalid(e) from e
    return Response(status_code=status.HTTP_204_NO_CONTENT)


async def _read_body(request: Request, max_bytes: int) -> bytes:
    """Тело целиком, но не больше `max_bytes`: заявленный размер проверяется до чтения,
    фактический — по ходу (Content-Length может не быть)."""
    too_large = SourceFileTooLargeError(f"файл больше {max_bytes} байт")
    declared = request.headers.get("content-length", "")
    if declared.isdigit() and int(declared) > max_bytes:
        raise too_large
    body = bytearray()
    async for chunk in request.stream():
        body += chunk
        if len(body) > max_bytes:
            raise too_large
    return bytes(body)


def _files(request: Request) -> SourceFileStore:
    files: SourceFileStore = request.app.state.source_files
    return files


def _file(info: SourceFileInfo) -> SourceFile:
    return SourceFile(name=info.name, size=info.size, modified_at=info.modified_at)


def _invalid(error: InvalidSourceError, *, with_details: bool = False) -> ApiError:
    details = [ErrorDetail(loc=list(p.loc), message=p.message) for p in error.problems]
    return ApiError(
        status.HTTP_422_UNPROCESSABLE_CONTENT,
        "invalid_input",
        str(error),
        details=details if with_details else None,
    )


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
