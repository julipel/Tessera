"""Коннектор `table`: CSV/XLSX → Entity по маппингу колонок, ошибки строк, курсор, кэш."""

import os
from decimal import Decimal
from io import BytesIO
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from openpyxl import Workbook
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.knowledge.public import (
    EntityItem,
    EntityRepository,
    MarkdownChunker,
    RawItemRef,
    SourceConnector,
    SourceFileError,
    SourceKind,
    SourceRecord,
    SourceRepository,
    SourceSpec,
    SourceSyncRecord,
    SourceSyncRepository,
    SqlSyncStore,
    SyncStatus,
    TableConnector,
    TableSourceError,
    run_sync,
)
from app.modules.shared.public import TenantId
from app.modules.tenants.public import SqlTenantDirectory
from knowledge_fakes import FakeEmbedder, InMemoryChunkIndex

CONFIG: dict[str, Any] = {
    "entity_type": "product",
    "currency": "rub",
    "columns": {
        "external_id": "Артикул",
        "title": "Название",
        "price": "Цена",
        "in_stock": "Наличие",
        "category": "Категория",
    },
    "attributes": {"color": "Цвет", "volume_ml": {"column": "Объём, мл", "type": "number"}},
}

CSV_HEADER = "Артикул;Название;Цена;Наличие;Категория;Цвет;Объём, мл\n"


def spec(config: dict[str, Any] = CONFIG) -> SourceSpec:
    return SourceSpec(TenantId(uuid4()), uuid4(), config)


def write(path: Path, data: str | bytes, mtime_ns: int | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data.encode() if isinstance(data, str) else data)
    if mtime_ns is not None:
        os.utime(path, ns=(mtime_ns, mtime_ns))


def xlsx(rows: list[list[Any]], sheet: str = "Каталог") -> bytes:
    workbook = Workbook()
    worksheet = workbook.active
    assert worksheet is not None
    worksheet.title = sheet
    for row in rows:
        worksheet.append(row)
    buffer = BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()


@pytest.fixture
def connector(tmp_path: Path) -> TableConnector:
    return TableConnector(tmp_path)


@pytest.fixture
def source() -> SourceSpec:
    return spec()


@pytest.fixture
def base(connector: TableConnector, source: SourceSpec) -> Path:
    path = connector.source_dir(source)
    path.mkdir(parents=True)
    return path


async def fetch_all(connector: TableConnector, source: SourceSpec) -> dict[str, EntityItem]:
    items = {}
    for ref in (await connector.discover(source)).refs:
        item = await connector.fetch(source, ref)
        assert isinstance(item, EntityItem)
        items[ref.external_id] = item
    return items


async def test_csv_rows_map_to_entities(
    connector: TableConnector, source: SourceSpec, base: Path
) -> None:
    rows = "A-1;Крем «Роза»;1 990,50 р.;да;Уход;розовый;50\nA-2;Тоник;по запросу;0;;;\n\n"
    write(base / "catalog.csv", (CSV_HEADER + rows).encode("cp1251"), mtime_ns=5_000)

    listing = await connector.discover(source)
    items = await fetch_all(connector, source)

    assert listing.refs == [RawItemRef("A-1", "5000"), RawItemRef("A-2", "5000")]
    assert listing.cursor == "5000"
    assert items["A-1"] == EntityItem(
        external_id="A-1",
        type="product",
        title="Крем «Роза»",
        price=Decimal("1990.50"),
        currency="RUB",
        in_stock=True,
        category="Уход",
        attributes={"color": "розовый", "volume_ml": 50},
    )
    assert items["A-2"] == EntityItem(
        external_id="A-2", type="product", title="Тоник", currency="RUB", in_stock=False
    )


async def test_xlsx_sheet_from_config_with_typed_cells(tmp_path: Path) -> None:
    connector = TableConnector(tmp_path)
    source = spec({**CONFIG, "sheet": "Каталог"})
    header = ["Артикул", "Название", "Цена", "Наличие", "Категория", "Цвет", "Объём, мл"]
    write(
        connector.source_dir(source) / "price.xlsx",
        xlsx([header, [12345.0, "Сыворотка", 2500.5, 3, "Уход", None, "30,5"]]),
    )

    items = await fetch_all(connector, source)

    assert items["12345"] == EntityItem(
        external_id="12345",
        type="product",
        title="Сыворотка",
        price=Decimal("2500.5"),
        currency="RUB",
        in_stock=True,
        category="Уход",
        attributes={"volume_ml": 30.5},
    )


@pytest.mark.parametrize(
    ("raw", "price"),
    [("1,990.50", "1990.50"), ("1.990,50", "1990.50"), ("12 000", "12000"), ("", None)],
)
async def test_price_formats(
    connector: TableConnector, source: SourceSpec, base: Path, raw: str, price: str | None
) -> None:
    write(base / "t.csv", f"{CSV_HEADER}A;Крем;{raw};;;;\n")

    item = (await fetch_all(connector, source))["A"]

    assert item.price == (Decimal(price) if price else None)


async def test_bad_rows_are_item_errors(
    connector: TableConnector, source: SourceSpec, base: Path
) -> None:
    write(
        base / "a.csv",
        CSV_HEADER
        + "A-1;Крем;100;да;;;\n"
        + ";Без артикула;100;да;;;\n"
        + "A-3;Цена;-5;да;;;\n"
        + "A-4;Наличие;100;возможно;;;\n"
        + "A-5;;100;да;;;\n",
    )
    write(base / "b.csv", CSV_HEADER + "A-1;Дубль;200;да;;;\nB-1;Норм;1;да;;;\n")

    refs = [r.external_id for r in (await connector.discover(source)).refs]
    assert refs == ["A-1", "a.csv#3", "A-3", "A-4", "A-5", "B-1"]

    expected = {
        "A-1": r"id «A-1» повторяется: a\.csv:2, b\.csv:2",
        "a.csv#3": r"a\.csv:3: пустой id",
        "A-3": r"a\.csv:4: цена: отрицательная",
        "A-4": r"a\.csv:5: не да/нет: «возможно»",
        "A-5": r"a\.csv:6: пустое название",
    }
    for external_id, message in expected.items():
        with pytest.raises(TableSourceError, match=message):
            await connector.fetch(source, RawItemRef(external_id))
    assert (await connector.fetch(source, RawItemRef("B-1"))).title == "Норм"


@pytest.mark.parametrize(
    ("config", "files", "message"),
    [
        ({"entity_type": "product"}, {"a.csv": CSV_HEADER}, "неверный конфиг"),
        ({**CONFIG, "sheet": "Нет такого"}, {"a.xlsx": xlsx([["x"]])}, "нет листа"),
        (CONFIG, {"a.csv": "Артикул;Название\nA;B\n"}, "нет колонок «Цена», «Наличие»"),
        (CONFIG, {"a.xlsx": b"not a zip"}, r"a\.xlsx: не удалось прочитать XLSX"),
    ],
)
async def test_source_level_errors_fail_listing(
    tmp_path: Path, config: dict[str, Any], files: dict[str, str | bytes], message: str
) -> None:
    connector = TableConnector(tmp_path)
    source = spec(config)
    for name, data in files.items():
        write(connector.source_dir(source) / name, data)

    with pytest.raises(TableSourceError, match=message):
        await connector.discover(source)


async def test_missing_source_dir_is_an_error(connector: TableConnector) -> None:
    with pytest.raises(SourceFileError):
        await connector.discover(spec())


async def test_changed_since_returns_rows_of_newer_files(
    connector: TableConnector, source: SourceSpec, base: Path
) -> None:
    write(base / "old.csv", CSV_HEADER + "O;Старый;1;;;;\n", mtime_ns=1_000)
    write(base / "new.csv", CSV_HEADER + "N;Новый;1;;;;\n", mtime_ns=2_000)
    write(base / "empty.csv", CSV_HEADER, mtime_ns=3_000)

    listing = await connector.changed_since(source, "1000")
    assert ([r.external_id for r in listing.refs], listing.cursor) == (["N"], "3000")

    nothing = await connector.changed_since(source, "3000")
    assert (nothing.refs, nothing.cursor) == ([], "3000")


async def test_changed_file_is_parsed_again(
    connector: TableConnector, source: SourceSpec, base: Path
) -> None:
    write(base / "t.csv", CSV_HEADER + "A;Было;1;;;;\n", mtime_ns=1_000)
    assert (await connector.fetch(source, RawItemRef("A"))).title == "Было"

    write(base / "t.csv", CSV_HEADER + "A;Стало;1;;;;\n", mtime_ns=2_000)
    assert (await connector.fetch(source, RawItemRef("A"))).title == "Стало"


async def test_symlinked_tables_are_ignored(
    tmp_path: Path, connector: TableConnector, source: SourceSpec, base: Path
) -> None:
    write(tmp_path / "outside.csv", CSV_HEADER + "X;Чужое;1;;;;\n")
    (base / "link.csv").symlink_to(tmp_path / "outside.csv")

    assert (await connector.discover(source)).refs == []


def test_satisfies_protocol(connector: TableConnector) -> None:
    proto: SourceConnector = connector
    assert proto.kind is SourceKind.TABLE


async def test_sync_stores_entities(tmp_path: Path, db_session: AsyncSession) -> None:
    tenant_id = (await SqlTenantDirectory(db_session).create("tables", "tables")).id
    source = await SourceRepository(db_session).add(
        tenant_id, SourceRecord(tenant_id=tenant_id, kind=SourceKind.TABLE, config=CONFIG)
    )
    connector = TableConnector(tmp_path)
    base = connector.source_dir(SourceSpec(tenant_id, source.id, CONFIG))
    write(base / "catalog.csv", CSV_HEADER + "A-1;Крем;1990;да;Уход;белый;50\nA-2;Тоник;;;;;\n")

    async def sync(full: bool) -> dict[str, Any]:
        record = await SourceSyncRepository(db_session).add(
            tenant_id, SourceSyncRecord(tenant_id=tenant_id, source_id=source.id)
        )
        sync_id = record.id
        await run_sync(
            tenant_id,
            sync_id,
            SqlSyncStore(db_session),
            {SourceKind.TABLE: connector},
            MarkdownChunker(),
            embedder=FakeEmbedder(),
            index=InMemoryChunkIndex(),
            full=full,
        )
        record = await SourceSyncRepository(db_session).get_or_raise(tenant_id, sync_id)
        await db_session.refresh(record)
        assert record.status is SyncStatus.SUCCEEDED
        return record.stats

    assert (await sync(full=True))["created"] == 2
    entities = {e.external_id: e for e in await EntityRepository(db_session).list(tenant_id)}
    assert entities["A-1"].price == Decimal("1990.00")
    assert entities["A-1"].attributes == {"color": "белый", "volume_ml": 50}
    assert (entities["A-2"].type, entities["A-2"].in_stock) == ("product", None)

    assert (await sync(full=True))["unchanged"] == 2
