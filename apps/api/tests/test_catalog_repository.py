"""Структурный поиск по каталогу (P4-08a, ADR-0003, ADR-0016): фильтры, полнотекстовый query,
сортировка, постраничность и изоляция тенантов — на реальном Postgres."""

from collections.abc import Sequence
from decimal import Decimal
from typing import Any
from uuid import uuid4

import pytest
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncSession, async_sessionmaker

from app.modules.knowledge.public import (
    AttributeFilter,
    CatalogError,
    CatalogFilters,
    CatalogQuery,
    CatalogSort,
    EntityRecord,
    EntityRepository,
    SourceKind,
    SourceRecord,
    SourceRepository,
    SqlCatalog,
)
from app.modules.shared.public import TenantId
from app.modules.tenants.public import SqlTenantDirectory

HASH = "0" * 64

PRODUCTS: list[dict[str, Any]] = [
    {
        "external_id": "cream-dry",
        "title": "Крем для сухой кожи",
        "price": Decimal("1990"),
        "in_stock": True,
        "category": "Уход за лицом",
        "attributes": {"skin_type": "Сухая", "volume_ml": 50, "vegan": True},
    },
    {
        "external_id": "cream-oily",
        "title": "Матирующий крем",
        "price": Decimal("1490"),
        "in_stock": True,
        "category": "уход за лицом",
        "attributes": {"skin_type": "Жирная", "volume_ml": 30, "vegan": False},
    },
    {
        "external_id": "serum",
        "title": "Сыворотка с витамином C",
        "price": Decimal("2590"),
        "in_stock": False,
        "category": "Уход за лицом",
        "attributes": {"skin_type": "Любая", "volume_ml": "30 мл", "brand": "Glow"},
    },
    {
        "external_id": "shampoo",
        "title": "Шампунь для сухих волос",
        "price": None,
        "in_stock": None,
        "category": "Волосы",
        "attributes": {"volume_ml": 250},
    },
    {
        "external_id": "massage",
        "type": "service",
        "title": "Массаж лица",
        "price": Decimal("3000"),
        "in_stock": True,
        "category": "Услуги",
        "attributes": {},
    },
]


async def _tenant(session: AsyncSession, slug: str) -> TenantId:
    return (await SqlTenantDirectory(session).create(slug, slug)).id


async def _seed(
    session: AsyncSession, tenant_id: TenantId, products: Sequence[dict[str, Any]] = PRODUCTS
) -> dict[str, EntityRecord]:
    source = await SourceRepository(session).add(
        tenant_id, SourceRecord(tenant_id=tenant_id, kind=SourceKind.TABLE, config={})
    )
    repo = EntityRepository(session)
    rows: dict[str, EntityRecord] = {}
    for product in products:
        record = EntityRecord(
            tenant_id=tenant_id,
            source_id=source.id,
            content_hash=HASH,
            **{"type": "product", **product},
        )
        rows[product["external_id"]] = await repo.add(tenant_id, record)
    return rows


@pytest.fixture
async def catalog(db_session: AsyncSession) -> tuple[TenantId, dict[str, EntityRecord]]:
    tenant_id = await _tenant(db_session, "catalog-a")
    return tenant_id, await _seed(db_session, tenant_id)


async def _ids(session: AsyncSession, tenant_id: TenantId, query: CatalogQuery) -> list[str]:
    page = await EntityRepository(session).search(tenant_id, query)
    by_id = {r.id: r.external_id for r in await EntityRepository(session).list(tenant_id)}
    return [by_id[item.id] for item in page.items]


def _filters(**kwargs: Any) -> CatalogQuery:
    return CatalogQuery(filters=CatalogFilters(**kwargs), sort=CatalogSort.TITLE)


async def test_without_filters_in_stock_first_then_title(
    db_session: AsyncSession, catalog: tuple[TenantId, Any]
) -> None:
    tenant_id, _ = catalog

    ids = await _ids(db_session, tenant_id, CatalogQuery())

    # В наличии по названию, затем нет в наличии и неизвестно — тоже по названию.
    assert ids == ["cream-dry", "massage", "cream-oily", "serum", "shampoo"]


@pytest.mark.parametrize(
    ("filters", "expected"),
    [
        ({"types": ["service"]}, ["massage"]),
        ({"categories": ["УХОД за ЛИЦОМ"]}, ["cream-dry", "cream-oily", "serum"]),
        ({"categories": ["волосы", "услуги"]}, ["massage", "shampoo"]),
        ({"price_min": Decimal("1990")}, ["cream-dry", "massage", "serum"]),
        ({"price_max": Decimal("1990")}, ["cream-dry", "cream-oily"]),
        ({"price_min": Decimal("1500"), "price_max": Decimal("2600")}, ["cream-dry", "serum"]),
        ({"in_stock": True}, ["cream-dry", "massage", "cream-oily"]),
        ({"in_stock": False}, ["serum"]),
        (
            {"types": ["product"], "in_stock": True, "price_max": Decimal("1500")},
            ["cream-oily"],
        ),
    ],
    ids=lambda v: repr(v) if isinstance(v, dict) else "",
)
async def test_normalized_field_filters(
    db_session: AsyncSession,
    catalog: tuple[TenantId, Any],
    filters: dict[str, Any],
    expected: list[str],
) -> None:
    tenant_id, _ = catalog
    assert await _ids(db_session, tenant_id, _filters(**filters)) == expected


@pytest.mark.parametrize(
    ("attributes", "expected"),
    [
        # Строки — без учёта регистра.
        ({"skin_type": AttributeFilter(any_of=["сухая"])}, ["cream-dry"]),
        (
            {"skin_type": AttributeFilter(any_of=["жирная", "ЛЮБАЯ"])},
            ["cream-oily", "serum"],
        ),
        # Число: 50 == 50.0; строка "30 мл" числом не считается.
        ({"volume_ml": AttributeFilter(any_of=[50.0])}, ["cream-dry"]),
        ({"volume_ml": AttributeFilter(any_of=[30])}, ["cream-oily"]),
        # Диапазон: нечисловое значение не совпадает и не роняет запрос.
        ({"volume_ml": AttributeFilter(min=Decimal(30))}, ["cream-dry", "cream-oily", "shampoo"]),
        ({"volume_ml": AttributeFilter(min=Decimal(31), max=Decimal(100))}, ["cream-dry"]),
        # Булево; строка "true" не совпала бы с True.
        ({"vegan": AttributeFilter(any_of=[True])}, ["cream-dry"]),
        ({"vegan": AttributeFilter(any_of=[False])}, ["cream-oily"]),
        # Строка не равна числу с тем же текстом.
        ({"volume_ml": AttributeFilter(any_of=["50"])}, []),
        # Несколько атрибутов — все условия.
        (
            {
                "skin_type": AttributeFilter(any_of=["сухая", "жирная"]),
                "volume_ml": AttributeFilter(max=Decimal(40)),
            },
            ["cream-oily"],
        ),
        # Атрибута нет у сущности — не совпадает.
        ({"brand": AttributeFilter(any_of=["glow"])}, ["serum"]),
        ({"missing": AttributeFilter(any_of=["x"])}, []),
    ],
)
async def test_attribute_filters(
    db_session: AsyncSession,
    catalog: tuple[TenantId, Any],
    attributes: dict[str, AttributeFilter],
    expected: list[str],
) -> None:
    tenant_id, _ = catalog
    assert await _ids(db_session, tenant_id, _filters(attributes=attributes)) == expected


LIST_PRODUCTS: list[dict[str, Any]] = [
    {
        "external_id": "cream",
        "title": "Крем",
        "attributes": {"skin_types": ["dry", "Sensitive"], "volumes": [30, 50], "flags": [True]},
    },
    {
        "external_id": "gel",
        "title": "Гель",
        "attributes": {"skin_types": ["oily"], "volumes": [50.5], "flags": [False]},
    },
    # Скаляр там, где у других массив, — по-прежнему совпадает по равенству.
    {"external_id": "toner", "title": "Тоник", "attributes": {"skin_types": "dry"}},
    # Число в массиве строкой не считается, и наоборот.
    {"external_id": "mask", "title": "Маска", "attributes": {"skin_types": [1], "volumes": ["30"]}},
]


@pytest.mark.parametrize(
    ("attributes", "expected"),
    [
        # Вхождение элемента; строки — без учёта регистра.
        ({"skin_types": AttributeFilter(any_of=["dry"])}, ["cream", "toner"]),
        ({"skin_types": AttributeFilter(any_of=["SENSITIVE"])}, ["cream"]),
        ({"skin_types": AttributeFilter(any_of=["sensitive", "oily"])}, ["gel", "cream"]),
        ({"skin_types": AttributeFilter(any_of=["normal"])}, []),
        ({"skin_types": AttributeFilter(any_of=["1"])}, []),
        ({"skin_types": AttributeFilter(any_of=[1])}, ["mask"]),
        # Числа: 50 == 50.0, строка "30" в массиве — не число.
        ({"volumes": AttributeFilter(any_of=[50.0])}, ["cream"]),
        ({"volumes": AttributeFilter(any_of=[30])}, ["cream"]),
        ({"volumes": AttributeFilter(any_of=[50.5])}, ["gel"]),
        ({"flags": AttributeFilter(any_of=[False])}, ["gel"]),
        # Диапазон к массиву не применяется.
        ({"volumes": AttributeFilter(min=Decimal(0))}, []),
    ],
)
async def test_list_attribute_filters(
    db_session: AsyncSession, attributes: dict[str, AttributeFilter], expected: list[str]
) -> None:
    tenant_id = await _tenant(db_session, "catalog-lists")
    await _seed(db_session, tenant_id, LIST_PRODUCTS)
    other = await _tenant(db_session, "catalog-lists-other")
    await _seed(db_session, other, LIST_PRODUCTS[:1])

    assert await _ids(db_session, tenant_id, _filters(attributes=attributes)) == expected


async def test_query_matches_list_attribute_strings(db_session: AsyncSession) -> None:
    tenant_id = await _tenant(db_session, "catalog-lists")
    await _seed(db_session, tenant_id, LIST_PRODUCTS)

    query = CatalogQuery(query="sensitive", sort=CatalogSort.TITLE)
    assert await _ids(db_session, tenant_id, query) == ["cream"]


async def test_attribute_key_is_not_sql(
    db_session: AsyncSession, catalog: tuple[TenantId, Any]
) -> None:
    tenant_id, _ = catalog
    key = "x' OR '1'='1"
    query = _filters(attributes={key: AttributeFilter(any_of=["x"])})
    assert await _ids(db_session, tenant_id, query) == []


def test_attribute_filter_requires_condition() -> None:
    with pytest.raises(ValueError):
        AttributeFilter()


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        # Стемминг: «сухих волос» находит и «сухой кожи», и «сухих волос».
        ("сухих волос", ["shampoo", "cream-dry"]),
        # Любое из слов; совпадение по категории и строковому атрибуту.
        ("крем", ["cream-oily", "cream-dry"]),
        ("волосы", ["shampoo"]),
        ("glow", ["serum"]),
        # Ключи атрибутов и числа не ищутся.
        ("skin_type", []),
        ("нет такого", []),
    ],
)
async def test_full_text_query(
    db_session: AsyncSession, catalog: tuple[TenantId, Any], text: str, expected: list[str]
) -> None:
    tenant_id, _ = catalog
    # Порядок проверяет test_query_rank_orders_within_stock_group, здесь — состав выдачи.
    ids = await _ids(db_session, tenant_id, CatalogQuery(query=text))
    assert sorted(ids) == sorted(expected)


async def test_query_rank_orders_within_stock_group(
    db_session: AsyncSession, catalog: tuple[TenantId, Any]
) -> None:
    tenant_id, _ = catalog
    query = CatalogQuery(query="крем для сухой кожи", filters=CatalogFilters(in_stock=True))

    ids = await _ids(db_session, tenant_id, query)

    assert ids == ["cream-dry", "cream-oily"]


async def test_stop_words_only_query_does_not_filter(
    db_session: AsyncSession, catalog: tuple[TenantId, Any]
) -> None:
    tenant_id, _ = catalog
    assert len(await _ids(db_session, tenant_id, CatalogQuery(query="для и"))) == 5
    assert len(await _ids(db_session, tenant_id, CatalogQuery(query="  "))) == 5


async def test_query_syntax_is_literal(
    db_session: AsyncSession, catalog: tuple[TenantId, Any]
) -> None:
    tenant_id, _ = catalog
    # Синтаксис tsquery от модели — просто текст, ошибки разбора нет.
    ids = await _ids(db_session, tenant_id, CatalogQuery(query="крем & !(| 'массаж"))
    assert sorted(ids) == ["cream-dry", "cream-oily", "massage"]


@pytest.mark.parametrize(
    ("sort", "expected"),
    [
        (CatalogSort.PRICE_ASC, ["cream-oily", "cream-dry", "massage", "serum", "shampoo"]),
        (CatalogSort.PRICE_DESC, ["massage", "cream-dry", "cream-oily", "serum", "shampoo"]),
        (CatalogSort.TITLE, ["cream-dry", "massage", "cream-oily", "serum", "shampoo"]),
        # Без query relevance — по названию.
        (CatalogSort.RELEVANCE, ["cream-dry", "massage", "cream-oily", "serum", "shampoo"]),
    ],
)
async def test_sort(
    db_session: AsyncSession,
    catalog: tuple[TenantId, Any],
    sort: CatalogSort,
    expected: list[str],
) -> None:
    tenant_id, _ = catalog
    assert await _ids(db_session, tenant_id, CatalogQuery(sort=sort)) == expected


async def test_limit_offset_and_total(
    db_session: AsyncSession, catalog: tuple[TenantId, Any]
) -> None:
    tenant_id, _ = catalog
    repo = EntityRepository(db_session)
    query = CatalogQuery(filters=CatalogFilters(types=["product"]), sort=CatalogSort.PRICE_ASC)

    first = await repo.search(tenant_id, CatalogQuery(**{**vars_of(query), "limit": 2}))
    second = await repo.search(
        tenant_id, CatalogQuery(**{**vars_of(query), "limit": 2, "offset": 2})
    )
    beyond = await repo.search(
        tenant_id, CatalogQuery(**{**vars_of(query), "limit": 2, "offset": 10})
    )
    empty = await repo.search(tenant_id, _filters(types=["nothing"]))

    assert [e.title for e in first.items] == ["Матирующий крем", "Крем для сухой кожи"]
    assert first.total == second.total == beyond.total == 4
    assert [e.title for e in second.items] == ["Сыворотка с витамином C", "Шампунь для сухих волос"]
    assert beyond.items == []
    assert empty.total == 0


def vars_of(query: CatalogQuery) -> dict[str, Any]:
    return {name: getattr(query, name) for name in CatalogQuery.__slots__}


def test_query_validates_paging() -> None:
    with pytest.raises(ValueError):
        CatalogQuery(limit=0)
    with pytest.raises(ValueError):
        CatalogQuery(offset=-1)


async def test_entity_fields_mapped(
    db_session: AsyncSession, catalog: tuple[TenantId, dict[str, EntityRecord]]
) -> None:
    tenant_id, rows = catalog
    page = await EntityRepository(db_session).search(tenant_id, _filters(in_stock=False))

    [serum] = page.items
    assert serum.id == rows["serum"].id
    assert serum.type == "product"
    assert serum.price == Decimal("2590.00")
    assert serum.category == "Уход за лицом"
    assert serum.attributes == {"skin_type": "Любая", "volume_ml": "30 мл", "brand": "Glow"}


async def test_search_and_get_isolate_tenants(
    db_session: AsyncSession, catalog: tuple[TenantId, dict[str, EntityRecord]]
) -> None:
    tenant_a, rows_a = catalog
    tenant_b = await _tenant(db_session, "catalog-b")
    rows_b = await _seed(
        db_session,
        tenant_b,
        [{"external_id": "b-cream", "title": "Крем тенанта B", "in_stock": True}],
    )
    repo = EntityRepository(db_session)

    page_a = await repo.search(tenant_a, CatalogQuery(query="крем", limit=50))
    page_b = await repo.search(tenant_b, CatalogQuery(limit=50))

    assert rows_b["b-cream"].id not in {e.id for e in page_a.items}
    assert page_a.total == 2
    assert [e.title for e in page_b.items] == ["Крем тенанта B"] and page_b.total == 1
    assert await repo.get_catalog_entity(tenant_a, rows_b["b-cream"].id) is None
    got = await repo.get_catalog_entity(tenant_a, rows_a["cream-dry"].id)
    assert got is not None and got.title == "Крем для сухой кожи"
    many = await repo.list_catalog_entities(
        tenant_a, [rows_a["cream-dry"].id, rows_b["b-cream"].id, uuid4()]
    )
    assert [e.title for e in many] == ["Крем для сухой кожи"]
    assert await repo.list_catalog_entities(tenant_a, []) == []


async def test_sql_catalog_uses_own_sessions(
    db_connection: AsyncConnection,
    db_session: AsyncSession,
    catalog: tuple[TenantId, dict[str, EntityRecord]],
) -> None:
    tenant_id, rows = catalog
    await db_session.flush()
    factory = async_sessionmaker(
        bind=db_connection, join_transaction_mode="create_savepoint", expire_on_commit=False
    )
    sql_catalog = SqlCatalog(factory)

    page = await sql_catalog.search(tenant_id, _filters(types=["service"]))
    entity = await sql_catalog.get(tenant_id, rows["massage"].id)

    assert [e.title for e in page.items] == ["Массаж лица"]
    assert entity is not None and entity.price == Decimal("3000.00")
    assert await sql_catalog.get(tenant_id, uuid4()) is None


class _BrokenSession:
    async def __aenter__(self) -> "_BrokenSession":
        raise OperationalError("SELECT", {}, Exception("connection refused"))

    async def __aexit__(self, *exc: object) -> None:
        return None


async def test_sql_catalog_wraps_db_errors() -> None:
    sql_catalog = SqlCatalog(lambda: _BrokenSession())  # type: ignore[arg-type, return-value]
    tenant_id = TenantId(uuid4())

    with pytest.raises(CatalogError):
        await sql_catalog.search(tenant_id, CatalogQuery())
    with pytest.raises(CatalogError):
        await sql_catalog.get(tenant_id, uuid4())
