"""Коннектор `table`: CSV/XLSX из каталога источника → Entity по маппингу колонок (ADR-0003).

Файлы — как у `file` (`source_files`, ADR-0012): все `*.csv` и `*.xlsx` каталога, первая
строка — заголовок. Строка таблицы — одна сущность, `external_id` — значение колонки id,
уникальное в пределах источника.

Таблица, которую не удалось прочитать или в которой нет колонки из маппинга, — ошибка
синхронизации, а не пропуск файла: иначе полный discover удалил бы её сущности. Ошибки
отдельных строк (пустой id, повтор id, неразбираемая цена) — ошибки элементов.
"""

import asyncio
import json
from collections import OrderedDict
from dataclasses import dataclass
from functools import partial
from pathlib import Path
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.modules.knowledge.domain.entities import SourceKind
from app.modules.knowledge.domain.ingestion import (
    EntityItem,
    Listing,
    RawItem,
    RawItemRef,
    SourceSpec,
)
from app.modules.knowledge.infrastructure.entity_mapping import (
    AttributeSpec,
    EntityFields,
    map_entity,
    text,
)
from app.modules.knowledge.infrastructure.source_files import (
    SourceFile,
    SourceFiles,
    files_cursor,
)
from app.modules.knowledge.infrastructure.table_readers import (
    READERS,
    Cell,
    ReadOptions,
    Row,
    TableReadError,
)
from app.modules.shared.kernel import TenantId

DEFAULT_MAX_FILE_BYTES = 50 * 1024 * 1024
DEFAULT_CACHE_SIZE = 4


class TableSourceError(Exception):
    """Ошибка источника целиком (конфиг, файл) или его строки."""


class _Config(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class TableColumns(EntityFields):
    """Нормализованное поле Entity → заголовок колонки."""


class AttributeColumn(_Config):
    column: str
    type: Literal["string", "number", "boolean"] = "string"

    def pair(self) -> tuple[str, Literal["string", "number", "boolean"]]:
        return self.column, self.type


class TableSourceConfig(_Config):
    """`Source.config` источника `table` (architecture.md §8)."""

    entity_type: str = Field(min_length=1, max_length=64)
    columns: TableColumns
    attributes: dict[str, str | AttributeColumn] = Field(default_factory=dict)
    currency: str | None = None  # если колонки валюты нет или ячейка пустая
    sheet: str | None = None  # XLSX; по умолчанию первый лист
    delimiter: str | None = Field(default=None, min_length=1, max_length=1)  # CSV

    def attribute_columns(self) -> dict[str, AttributeSpec]:
        return {
            name: AttributeSpec(spec) if isinstance(spec, str) else AttributeSpec(*spec.pair())
            for name, spec in self.attributes.items()
        }


@dataclass(frozen=True, slots=True)
class _Row:
    """Сущность строки или ошибка, которую `fetch` отдаст как ошибку элемента."""

    file: SourceFile
    item: EntityItem | None
    error: str | None = None


@dataclass(frozen=True, slots=True)
class _Table:
    signature: tuple[object, ...]
    files: list[SourceFile]
    rows: dict[str, _Row]


class TableConnector:
    kind = SourceKind.TABLE

    def __init__(
        self,
        root: Path,
        *,
        max_file_bytes: int = DEFAULT_MAX_FILE_BYTES,
        cache_size: int = DEFAULT_CACHE_SIZE,
    ) -> None:
        self.files = SourceFiles(root, READERS.keys())
        self.max_file_bytes = max_file_bytes
        self.cache_size = cache_size
        # fetch вызывается на каждую строку: разобранные таблицы источника кэшируются,
        # пока не изменились файлы или конфиг.
        self._cache: OrderedDict[tuple[TenantId, UUID], _Table] = OrderedDict()

    def source_dir(self, source: SourceSpec) -> Path:
        return self.files.source_dir(source)

    async def discover(self, source: SourceSpec) -> Listing:
        table = await asyncio.to_thread(self._load, source)
        return _listing(table)

    async def changed_since(self, source: SourceSpec, cursor: str) -> Listing:
        since = int(cursor)
        # Читаются все файлы: повтор id ищется по всему источнику.
        table = await asyncio.to_thread(self._load, source)
        return _listing(table, since=since, fallback=cursor)

    async def fetch(self, source: SourceSpec, ref: RawItemRef) -> RawItem:
        table = await asyncio.to_thread(self._load, source)
        row = table.rows.get(ref.external_id)
        if row is None:
            raise TableSourceError(f"строка не найдена: {ref.external_id}")
        if row.item is None:
            raise TableSourceError(row.error)
        return row.item

    def _load(self, source: SourceSpec) -> _Table:
        config = _parse_config(source.config)
        files = self.files.scan(source)
        signature = (
            json.dumps(source.config, sort_keys=True, default=str),
            tuple((f.external_id, f.mtime_ns, f.size) for f in files),
        )
        key = (source.tenant_id, source.source_id)
        cached = self._cache.get(key)
        if cached is not None and cached.signature == signature:
            self._cache.move_to_end(key)
            return cached
        table = _Table(signature, files, self._parse(config, files))
        self._cache[key] = table
        self._cache.move_to_end(key)
        while len(self._cache) > self.cache_size:
            self._cache.popitem(last=False)
        return table

    def _parse(self, config: TableSourceConfig, files: list[SourceFile]) -> dict[str, _Row]:
        options = ReadOptions(sheet=config.sheet, delimiter=config.delimiter)
        rows: dict[str, _Row] = {}
        first_line: dict[str, str] = {}
        for file in files:
            if file.size > self.max_file_bytes:
                raise TableSourceError(
                    f"файл больше {self.max_file_bytes} байт: {file.external_id}"
                )
            reader = READERS[file.path.suffix.lower()]
            try:
                data = reader(file.path.read_bytes(), options)
            except TableReadError as e:
                raise TableSourceError(f"{file.external_id}: {e}") from e
            for line, external_id, row in _map_rows(config, file, data):
                where = f"{file.external_id}:{line}"
                if external_id not in rows:
                    rows[external_id] = row
                    first_line[external_id] = where
                    continue
                # Какая из строк с одним id верная, неизвестно: ошибка элемента для обеих.
                error = f"id «{external_id}» повторяется: {first_line[external_id]}, {where}"
                rows[external_id] = _Row(rows[external_id].file, None, error)
        return rows


def _parse_config(config: dict[str, Any]) -> TableSourceConfig:
    try:
        return TableSourceConfig.model_validate(config)
    except ValidationError as e:
        raise TableSourceError(f"неверный конфиг источника table: {e}") from e


def _map_rows(
    config: TableSourceConfig, file: SourceFile, data: list[Row]
) -> list[tuple[int, str, _Row]]:
    """Строки таблицы → (номер строки в файле, external_id, _Row). Пустые строки пропускаются."""
    if not data:
        return []
    header: dict[str, int] = {}
    for index, name in enumerate(data[0]):
        name_text = text(name)
        if name_text is not None:
            header.setdefault(name_text, index)
    attributes = config.attribute_columns()
    wanted = [c for c in config.columns.model_dump().values() if c is not None]
    missing = [c for c in [*wanted, *(a.key for a in attributes.values())] if c not in header]
    if missing:
        names = ", ".join(f"«{c}»" for c in dict.fromkeys(missing))
        raise TableSourceError(f"{file.external_id}: нет колонок {names}")

    result: list[tuple[int, str, _Row]] = []
    for line, cells in enumerate(data[1:], start=2):
        if all(text(c) is None for c in cells):
            continue
        columns = config.columns
        where = f"{file.external_id}:{line}"
        external_id = text(_cell(cells, header, columns.external_id))
        if external_id is None:
            error = f"{where}: пустой id (колонка «{columns.external_id}»)"
            result.append((line, f"{file.external_id}#{line}", _Row(file, None, error)))
            continue
        try:
            item = map_entity(
                partial(_cell, cells, header),
                entity_type=config.entity_type,
                external_id=external_id,
                fields=columns,
                attributes=attributes,
                currency=config.currency,
            )
        except ValueError as e:
            result.append((line, external_id, _Row(file, None, f"{where}: {e}")))
            continue
        result.append((line, external_id, _Row(file, item)))
    return result


def _cell(cells: Row, header: dict[str, int], column: str) -> Cell:
    index = header[column]
    return cells[index] if index < len(cells) else None


def _listing(table: _Table, *, since: int | None = None, fallback: str | None = None) -> Listing:
    refs = [
        RawItemRef(external_id, str(row.file.mtime_ns))
        for external_id, row in table.rows.items()
        if since is None or row.file.mtime_ns > since
    ]
    changed = [f for f in table.files if since is None or f.mtime_ns > since]
    return Listing(refs, files_cursor(changed, fallback))
