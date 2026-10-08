"""Источники в админке (ADR-0037): список, создание, запуск синхронизации, история, файлы.

Создаются только website, file и table — у http_api и database секреты из env, общие для
всех тенантов (ADR-0017). Источники из YAML тенанта админка не меняет: их перезапишет
`make seed`. Конфиг — YAML, его разбирает бэкенд (как AgentConfig в P8-02a).
"""

import asyncio
import unicodedata
from collections.abc import Callable
from pathlib import PurePosixPath
from typing import Any
from uuid import UUID

import yaml

from app.modules.knowledge.application.sync_requests import request_sync
from app.modules.knowledge.domain.entities import SourceKind, SourceOrigin
from app.modules.knowledge.domain.errors import (
    InvalidSourceError,
    SourceFileTooLargeError,
    SourceProblem,
)
from app.modules.knowledge.domain.ingestion import SourceSpec
from app.modules.knowledge.domain.ports import (
    SourceAdminStore,
    SourceFileStore,
    SyncQueue,
    SyncStore,
)
from app.modules.knowledge.domain.source_admin import (
    ADMIN_SOURCE_KINDS,
    FILE_SUFFIXES,
    RECENT_SYNCS,
    SourceFileInfo,
    SourceFileLimits,
    SourceOverview,
    SyncRun,
)
from app.modules.shared.kernel import NotFoundError, TenantId

# Проверка конфига моделью коннектора; ошибки — InvalidSourceError с местами в конфиге.
type ConfigValidator = Callable[[SourceKind, dict[str, Any]], None]

_CONFIG = "config_yaml"


def parse_source_config_yaml(source: str) -> dict[str, Any]:
    """Пустой текст — пустой конфиг (у `file` настроек нет)."""
    try:
        data = yaml.safe_load(source)
    except yaml.YAMLError as e:
        mark = getattr(e, "problem_mark", None)
        where = f"строка {mark.line + 1}, столбец {mark.column + 1}: " if mark else ""
        problem = getattr(e, "problem", None) or str(e)
        message = f"YAML: {where}{problem}"
        raise InvalidSourceError(message, [SourceProblem((_CONFIG,), message)]) from e
    if data is None:
        return {}
    if not isinstance(data, dict):
        message = "конфиг источника — объект"
        raise InvalidSourceError(message, [SourceProblem((_CONFIG,), message)])
    return data


def source_config_yaml(config: dict[str, Any]) -> str:
    return yaml.safe_dump(config, allow_unicode=True, sort_keys=False, width=100) if config else ""


async def list_sources(tenant_id: TenantId, store: SourceAdminStore) -> list[SourceOverview]:
    return await store.list_sources(tenant_id)


async def get_source(
    tenant_id: TenantId, source_id: UUID, store: SourceAdminStore
) -> SourceOverview:
    source = await store.get_source(tenant_id, source_id)
    if source is None:
        raise NotFoundError(f"Source {source_id} not found")
    return source


async def create_source(
    tenant_id: TenantId,
    name: str,
    kind: SourceKind,
    config_yaml: str,
    store: SourceAdminStore,
    validate: ConfigValidator,
) -> SourceOverview:
    if kind not in ADMIN_SOURCE_KINDS:
        message = f"источник {kind} объявляется в YAML тенанта: он ссылается на секреты платформы"
        raise InvalidSourceError(message, [SourceProblem(("kind",), message)])
    config = parse_source_config_yaml(config_yaml)
    try:
        validate(kind, config)
    except InvalidSourceError as e:
        raise InvalidSourceError(
            str(e), [SourceProblem((_CONFIG, *p.loc), p.message) for p in e.problems]
        ) from e
    source_id = await store.create_source(tenant_id, name, kind, config)
    if source_id is None:
        message = f"имя {name!r} уже занято другим источником тенанта"
        raise InvalidSourceError(message, [SourceProblem(("name",), message)])
    await store.commit()
    return await get_source(tenant_id, source_id, store)


async def list_syncs(
    tenant_id: TenantId, source_id: UUID, store: SourceAdminStore
) -> list[SyncRun]:
    await get_source(tenant_id, source_id, store)
    return await store.recent_syncs(tenant_id, source_id, RECENT_SYNCS)


async def start_sync(
    tenant_id: TenantId,
    source_id: UUID,
    store: SourceAdminStore,
    syncs: SyncStore,
    queue: SyncQueue,
    *,
    full: bool,
) -> SyncRun:
    """Синхронизация в очереди воркера. Уже идущая возвращается та же (`request_sync`)."""
    sync_id = await request_sync(tenant_id, source_id, syncs, queue, full=full)
    run = await store.get_sync(tenant_id, sync_id)
    if run is None:
        raise NotFoundError(f"SourceSync {sync_id} not found")
    return run


# --- файлы источников file/table ---

MAX_FILE_NAME_BYTES = 255


def check_file_name(kind: SourceKind, name: str) -> None:
    """Имя файла в корне каталога источника: один сегмент пути, не скрытый, расширение
    читает коннектор вида."""
    if (
        not name
        or len(name.encode()) > MAX_FILE_NAME_BYTES
        or name != PurePosixPath(name).name
        or "\\" in name
        or name.startswith(".")
        or any(unicodedata.category(c) == "Cc" for c in name)
    ):
        raise InvalidSourceError(f"недопустимое имя файла: {name!r}")
    suffixes = FILE_SUFFIXES[kind]
    if PurePosixPath(name).suffix.lower() not in suffixes:
        allowed = ", ".join(sorted(suffixes))
        raise InvalidSourceError(f"источник {kind} читает только файлы {allowed}")


async def list_source_files(
    tenant_id: TenantId, source_id: UUID, store: SourceAdminStore, files: SourceFileStore
) -> list[SourceFileInfo]:
    source = _with_files(await get_source(tenant_id, source_id, store))
    return await asyncio.to_thread(files.list_files, _spec(tenant_id, source))


async def upload_source_file(
    tenant_id: TenantId,
    source_id: UUID,
    name: str,
    data: bytes,
    store: SourceAdminStore,
    files: SourceFileStore,
    limits: SourceFileLimits,
) -> SourceFileInfo:
    """Файл с тем же именем заменяется. Синхронизацию загрузка не запускает."""
    source = _editable_files(await get_source(tenant_id, source_id, store))
    check_file_name(source.kind, name)
    if not data:
        raise InvalidSourceError("файл пустой")
    if len(data) > limits.max_bytes:
        raise SourceFileTooLargeError(f"файл больше {limits.max_bytes} байт")
    spec = _spec(tenant_id, source)
    existing = await asyncio.to_thread(files.list_files, spec)
    if len(existing) >= limits.max_files and all(f.name != name for f in existing):
        raise InvalidSourceError(f"у источника уже {limits.max_files} файлов — больше нельзя")
    return await asyncio.to_thread(files.put_file, spec, name, data)


async def delete_source_file(
    tenant_id: TenantId,
    source_id: UUID,
    name: str,
    store: SourceAdminStore,
    files: SourceFileStore,
) -> None:
    """Данные файла удалит следующая полная синхронизация."""
    source = _editable_files(await get_source(tenant_id, source_id, store))
    check_file_name(source.kind, name)
    if not await asyncio.to_thread(files.delete_file, _spec(tenant_id, source), name):
        raise NotFoundError(f"файл {name!r} не найден")


def _with_files(source: SourceOverview) -> SourceOverview:
    if source.kind not in FILE_SUFFIXES:
        raise InvalidSourceError(f"у источника {source.kind} нет файлов")
    return source


def _editable_files(source: SourceOverview) -> SourceOverview:
    _with_files(source)
    if source.origin is not SourceOrigin.ADMIN:
        # Каталог источника из YAML зеркалирует make seed: загруженное он бы удалил.
        raise InvalidSourceError("файлы источника из YAML тенанта меняются через make seed")
    return source


def _spec(tenant_id: TenantId, source: SourceOverview) -> SourceSpec:
    return SourceSpec(tenant_id, source.id, source.config)
