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
import re
from collections import OrderedDict
from dataclasses import dataclass
from datetime import date, datetime, time
from decimal import Decimal, InvalidOperation
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


class TableColumns(_Config):
    """Нормализованное поле Entity → заголовок колонки."""

    external_id: str
    title: str
    price: str | None = None
    currency: str | None = None
    in_stock: str | None = None
    category: str | None = None
    url: str | None = None
    image_url: str | None = None


class AttributeColumn(_Config):
    column: str
    type: Literal["string", "number", "boolean"] = "string"


class TableSourceConfig(_Config):
    """`Source.config` источника `table` (architecture.md §8)."""

    entity_type: str = Field(min_length=1, max_length=64)
    columns: TableColumns
    attributes: dict[str, str | AttributeColumn] = Field(default_factory=dict)
    currency: str | None = None  # если колонки валюты нет или ячейка пустая
    sheet: str | None = None  # XLSX; по умолчанию первый лист
    delimiter: str | None = Field(default=None, min_length=1, max_length=1)  # CSV

    def attribute_columns(self) -> dict[str, AttributeColumn]:
        return {
            name: AttributeColumn(column=spec) if isinstance(spec, str) else spec
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
        text = _text(name)
        if text is not None:
            header.setdefault(text, index)
    attributes = config.attribute_columns()
    wanted = [c for c in config.columns.model_dump().values() if c is not None]
    missing = [c for c in [*wanted, *(a.column for a in attributes.values())] if c not in header]
    if missing:
        names = ", ".join(f"«{c}»" for c in dict.fromkeys(missing))
        raise TableSourceError(f"{file.external_id}: нет колонок {names}")

    result: list[tuple[int, str, _Row]] = []
    for line, cells in enumerate(data[1:], start=2):
        if all(_text(c) is None for c in cells):
            continue
        cell = partial(_cell, cells, header)
        columns = config.columns
        where = f"{file.external_id}:{line}"
        external_id = _text(cell(columns.external_id))
        if external_id is None:
            error = f"{where}: пустой id (колонка «{columns.external_id}»)"
            result.append((line, f"{file.external_id}#{line}", _Row(file, None, error)))
            continue
        try:
            title = _text(cell(columns.title))
            if title is None:
                raise ValueError(f"пустое название (колонка «{columns.title}»)")
            currency = _currency(_text(cell(columns.currency)) or config.currency)
            item = EntityItem(
                external_id=external_id,
                type=config.entity_type,
                title=title,
                price=_price(cell(columns.price)),
                currency=currency,
                in_stock=_bool(cell(columns.in_stock)),
                category=_text(cell(columns.category)),
                url=_text(cell(columns.url)),
                image_url=_text(cell(columns.image_url)),
                attributes={
                    name: value
                    for name, spec in attributes.items()
                    if (value := _attribute(cell(spec.column), spec.type)) is not None
                },
            )
        except ValueError as e:
            result.append((line, external_id, _Row(file, None, f"{where}: {e}")))
            continue
        result.append((line, external_id, _Row(file, item)))
    return result


def _cell(cells: Row, header: dict[str, int], column: str | None) -> Cell:
    if column is None:
        return None
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


# --- значения ячеек ---------------------------------------------------------------------------

_TRUE = {"да", "true", "yes", "y", "+", "есть", "в наличии", "имеется"}
_FALSE = {"нет", "false", "no", "n", "-", "\u2212", "—"}
_FALSE |= {"нет в наличии", "отсутствует", "под заказ"}
_NOT_NUMBER = re.compile(r"[^\d.,\-]")


def _text(value: Cell) -> str | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, float) and value.is_integer():
        return str(int(value))  # артикул 12345 из XLSX приходит как 12345.0
    if isinstance(value, datetime | date | time):
        return value.isoformat()
    text = str(value).strip()
    return text or None


def _decimal(value: Cell) -> Decimal | None:
    """`1 990,50 ₽`, `1,990.50`, `1990.5` → Decimal. Одиночная запятая — десятичная.
    Текст без цифр («по запросу», «договорная») — значение неизвестно (None)."""
    if isinstance(value, bool | datetime | date | time):
        raise ValueError(f"не число: {value}")
    if isinstance(value, int | float):
        return Decimal(str(value))
    text = _text(value)
    if text is None:
        return None
    raw = _NOT_NUMBER.sub("", text).strip(".,")  # «1990 р.»
    if not any(c.isdigit() for c in raw):
        return None
    if "," in raw and "." in raw:
        thousands = "," if raw.rindex(",") < raw.rindex(".") else "."
        raw = raw.replace(thousands, "")
    if raw.count(",") > 1 or raw.count(".") > 1:
        raw = raw.replace(",", "").replace(".", "")
    try:
        number = Decimal(raw.replace(",", "."))
    except InvalidOperation:
        raise ValueError(f"не число: «{text}»") from None
    if not number.is_finite():
        raise ValueError(f"не число: «{text}»")
    return number


def _currency(code: str | None) -> str | None:
    if code is None:
        return None
    if not re.fullmatch(r"[A-Za-z]{3}", code):
        raise ValueError(f"валюта: «{code}» — не код ISO 4217 (RUB, EUR…)")
    return code.upper()


def _price(value: Cell) -> Decimal | None:
    try:
        price = _decimal(value)
    except ValueError as e:
        raise ValueError(f"цена: {e}") from None
    if price is not None and price < 0:
        raise ValueError(f"цена: отрицательная ({price})")
    return price


def _bool(value: Cell) -> bool | None:
    """Да/нет, true/false, +/-, «в наличии»; число — остаток: больше нуля → True."""
    if value is None or isinstance(value, bool):
        return value
    text = _text(value)
    if text is None:
        return None
    if text.lower() in _TRUE:
        return True
    if text.lower() in _FALSE:
        return False
    try:
        number = _decimal(value)
    except ValueError:
        number = None
    if number is None:
        raise ValueError(f"не да/нет: «{text}»")
    return number > 0


def _attribute(value: Cell, kind: str) -> str | int | float | bool | None:
    if kind == "boolean":
        return _bool(value)
    if kind == "number":
        number = _decimal(value)
        if number is None:
            return None
        return int(number) if number == number.to_integral_value() else float(number)
    return _text(value)
