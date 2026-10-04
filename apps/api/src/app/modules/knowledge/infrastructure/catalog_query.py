"""SQL-выражения структурного поиска по каталогу (ADR-0003, ADR-0016) для
`EntityRepository.search`: фильтры по нормализованным полям и `attributes`, полнотекстовый
`query`, сортировка.
"""

from typing import Any

from sqlalchemy import (
    ColumnElement,
    Numeric,
    Text,
    and_,
    case,
    cast,
    exists,
    false,
    func,
    literal,
    literal_column,
    or_,
    select,
)
from sqlalchemy.dialects.postgresql import TSQUERY

from app.modules.knowledge.domain.catalog import (
    AttributeFilter,
    AttributeValue,
    CatalogEntity,
    CatalogFilters,
    CatalogSort,
)
from app.modules.knowledge.infrastructure.models import EntityRecord

_RUSSIAN: ColumnElement[Any] = literal_column("'russian'::regconfig")
_STRING_VALUES: ColumnElement[Any] = literal_column("""'["string"]'::jsonb""")


def to_catalog_entity(record: EntityRecord) -> CatalogEntity:
    return CatalogEntity(
        id=record.id,
        type=record.type,
        title=record.title,
        price=record.price,
        currency=record.currency,
        in_stock=record.in_stock,
        category=record.category,
        url=record.url,
        image_url=record.image_url,
        attributes=dict(record.attributes),
    )


def entity_tsvector() -> ColumnElement[Any]:
    """Название, категория и строковые значения атрибутов (ключи и числа — нет)."""
    text = func.to_tsvector(
        _RUSSIAN, func.concat_ws(" ", EntityRecord.title, EntityRecord.category)
    )
    attributes = func.jsonb_to_tsvector(_RUSSIAN, EntityRecord.attributes, _STRING_VALUES)
    return text.op("||")(attributes)


def any_word_tsquery(query: str) -> ColumnElement[Any]:
    """`plainto_tsquery` (стемминг, стоп-слова, синтаксис tsquery от модели не нужен) с заменой
    И на ИЛИ: «крем для сухой кожи» находит и товары, где есть не все слова, — их порядок
    задаёт `ts_rank`. Лексемы `&` не содержат: парсер считает его разделителем."""
    words = func.plainto_tsquery(_RUSSIAN, query)
    return cast(func.replace(cast(words, Text), "&", "|"), TSQUERY)


def filter_criteria(filters: CatalogFilters) -> list[ColumnElement[bool]]:
    entity = EntityRecord
    criteria: list[ColumnElement[bool]] = []
    if filters.types:
        criteria.append(entity.type.in_(filters.types))
    if filters.categories:
        criteria.append(func.lower(entity.category).in_([c.lower() for c in filters.categories]))
    if filters.price_min is not None:
        criteria.append(entity.price >= filters.price_min)
    if filters.price_max is not None:
        criteria.append(entity.price <= filters.price_max)
    if filters.in_stock is not None:
        criteria.append(entity.in_stock.is_(filters.in_stock))
    for key, condition in filters.attributes.items():
        criteria.append(_attribute_criterion(key, condition))
    return criteria


def order_by(sort: CatalogSort, rank: ColumnElement[Any] | None) -> list[Any]:
    """В наличии — выше (нет данных о наличии — как «нет»), затем `sort`, затем название и id:
    порядок детерминирован и для постраничной выдачи."""
    entity = EntityRecord
    order: list[Any] = [case((entity.in_stock.is_(True), 0), else_=1)]
    match sort:
        case CatalogSort.PRICE_ASC:
            order.append(entity.price.asc().nulls_last())
        case CatalogSort.PRICE_DESC:
            order.append(entity.price.desc().nulls_last())
        case CatalogSort.RELEVANCE if rank is not None:
            order.append(rank.desc())
        case _:
            pass
    order += [func.lower(entity.title), entity.id]
    return order


def _attribute_criterion(key: str, condition: AttributeFilter) -> ColumnElement[bool]:
    element = EntityRecord.attributes[key]  # ключ — параметр запроса, не текст SQL
    kind = func.jsonb_typeof(element)
    parts: list[ColumnElement[bool]] = []
    if condition.any_of:
        parts.append(or_(*(_attribute_equals(element, kind, v) for v in condition.any_of)))
    if condition.min is not None or condition.max is not None:
        number = _attribute_number(element, kind)
        if condition.min is not None:
            parts.append(number >= condition.min)
        if condition.max is not None:
            parts.append(number <= condition.max)
    return and_(*parts)


def _attribute_number(element: ColumnElement[Any], kind: ColumnElement[Any]) -> ColumnElement[Any]:
    # CASE, а не AND: приведение к numeric не должно выполняться для нечисловых значений.
    return case((kind == "number", cast(element.astext, Numeric)), else_=None)


def _attribute_equals(
    element: ColumnElement[Any], kind: ColumnElement[Any], value: AttributeValue
) -> ColumnElement[bool]:
    """Скаляр равен значению или массив (атрибут `list`) содержит его."""
    if isinstance(value, str):
        scalar = and_(kind == "string", func.lower(element.astext) == value.lower())
        return or_(scalar, _array_has_string(element, kind, value))
    if isinstance(value, bool):
        scalar = and_(kind == "boolean", element.astext == ("true" if value else "false"))
    else:
        scalar = _attribute_number(element, kind) == value
    # jsonb @> безопасен для любого типа: скаляр не содержит массив; числа — по значению.
    contains = element.op("@>")(func.jsonb_build_array(value))
    return or_(scalar, contains)


def _array_has_string(
    element: ColumnElement[Any], kind: ColumnElement[Any], value: str
) -> ColumnElement[bool]:
    """Строковый элемент массива без учёта регистра, как у скаляра. CASE: разворачивать
    не-массив `jsonb_array_elements` нельзя — ошибка."""
    items = func.jsonb_array_elements(element).table_valued("value").alias("item")
    match = exists(
        select(literal(1))
        .select_from(items)
        .where(
            func.jsonb_typeof(items.c.value) == "string",
            func.lower(items.c.value.op("#>>")(literal_column("'{}'"))) == value.lower(),
        )
    )
    return case((kind == "array", match), else_=false())
