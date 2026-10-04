"""Структурный поиск по каталогу Entity (architecture.md §8, ADR-0003, ADR-0016).

Фильтры — по нормализованным полям и `attributes`; `query` — полнотекстовый поиск Postgres
по названию, категории и строковым атрибутам (векторного поиска по сущностям пока нет).
Строки сравниваются без учёта регистра.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from enum import StrEnum
from typing import Any
from uuid import UUID

type AttributeValue = str | int | float | bool


@dataclass(frozen=True, slots=True)
class AttributeFilter:
    """Условие на один атрибут: совпадение с любым из `any_of` и/или числовой диапазон
    `[min, max]`. Значение атрибута другого типа (строка при диапазоне) не совпадает."""

    any_of: Sequence[AttributeValue] = ()
    min: Decimal | None = None
    max: Decimal | None = None

    def __post_init__(self) -> None:
        if not self.any_of and self.min is None and self.max is None:
            raise ValueError("AttributeFilter: нужно значение или диапазон")


@dataclass(frozen=True, slots=True)
class CatalogFilters:
    """Пустые поля не фильтруют. `categories` и `types` — любое из перечисленных."""

    types: Sequence[str] = ()
    categories: Sequence[str] = ()
    price_min: Decimal | None = None
    price_max: Decimal | None = None
    in_stock: bool | None = None
    attributes: Mapping[str, AttributeFilter] = field(default_factory=dict)


class CatalogSort(StrEnum):
    """`relevance` — по рангу `query` (без запроса — по названию). Сущности в наличии всегда
    выше; сущности без цены при сортировке по цене — в конце."""

    RELEVANCE = "relevance"
    PRICE_ASC = "price_asc"
    PRICE_DESC = "price_desc"
    TITLE = "title"


@dataclass(frozen=True, slots=True)
class CatalogQuery:
    query: str | None = None
    filters: CatalogFilters = field(default_factory=CatalogFilters)
    sort: CatalogSort = CatalogSort.RELEVANCE
    limit: int = 10
    offset: int = 0

    def __post_init__(self) -> None:
        if self.limit < 1 or self.offset < 0:
            raise ValueError("CatalogQuery: limit ≥ 1, offset ≥ 0")


@dataclass(frozen=True, slots=True)
class CatalogEntity:
    id: UUID
    type: str
    title: str
    price: Decimal | None = None
    currency: str | None = None
    in_stock: bool | None = None
    category: str | None = None
    url: str | None = None
    image_url: str | None = None
    attributes: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class CatalogPage:
    """`total` — сколько всего сущностей подходит под запрос (без limit/offset)."""

    items: Sequence[CatalogEntity]
    total: int
