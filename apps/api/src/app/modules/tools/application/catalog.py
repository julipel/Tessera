"""Встроенные инструменты каталога (contracts.md §4): `search_catalog` — структурный поиск
Entity через порт `Catalog` (реализация — knowledge `SqlCatalog`, ADR-0003, ADR-0016),
`get_entity` — детали одной сущности.

Фильтровать по `attributes` можно только по `knowledge.catalog.filterable_attributes`
конфига: схема аргументов перечисляет их, лишний атрибут реестр отклоняет
(`validation_error`). UI-компонентов инструменты не отдают — карточки показывает
`show_entities` по id из выдачи.
"""

from collections.abc import Iterable, Mapping
from decimal import Decimal
from typing import Any
from uuid import UUID

import structlog

from app.contracts import CatalogConfig
from app.modules.knowledge.kernel import (
    AttributeFilter,
    CatalogEntity,
    CatalogError,
    CatalogFilters,
    CatalogQuery,
    CatalogSort,
)
from app.modules.tools.domain.definition import ToolContext, ToolDefinition, ToolHandler
from app.modules.tools.domain.ports import Catalog
from app.modules.tools.domain.result import ToolError, ToolResult

logger = structlog.get_logger(__name__)

SEARCH_CATALOG = "search_catalog"
GET_ENTITY = "get_entity"
DEFAULT_LIMIT = 10
LIMIT_MAX = 20

_SEARCH_DESCRIPTION = (
    "Найди товары и услуги в каталоге компании. Используй для подбора, проверки наличия, "
    "цен и характеристик — не выдумывай товары, которых нет в выдаче. Сужай выдачу "
    "фильтрами (цена, наличие, категория, атрибуты), а query — для слов из названия или "
    "описания («крем для сухой кожи»). Если ничего не нашлось — ослабь фильтры. Отвечай "
    "только по найденным позициям; id из выдачи передавай в другие инструменты."
)
_GET_DESCRIPTION = (
    "Получи все данные одной позиции каталога по id из выдачи search_catalog: цену, "
    "наличие, ссылку и характеристики."
)
_SCALAR: dict[str, Any] = {"type": ["string", "number", "boolean"]}


def search_catalog_tool(catalog: Catalog, settings: CatalogConfig | None = None) -> ToolDefinition:
    attributes = (settings.filterable_attributes if settings else None) or []
    return ToolDefinition(
        name=SEARCH_CATALOG,
        description=_SEARCH_DESCRIPTION,
        parameters={
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "minLength": 1,
                    "description": "Слова для поиска по названию, категории и характеристикам.",
                },
                "filters": _filters_schema(attributes),
                "sort": {
                    "type": "string",
                    "enum": [s.value for s in CatalogSort],
                    "description": "Порядок: relevance (по query), price_asc, price_desc, title.",
                },
                "limit": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": LIMIT_MAX,
                    "description": f"Сколько позиций вернуть (по умолчанию {DEFAULT_LIMIT}).",
                },
            },
            "additionalProperties": False,
        },
        handler=_search_handler(catalog),
        display_label="Ищу в каталоге",
    )


def get_entity_tool(catalog: Catalog) -> ToolDefinition:
    return ToolDefinition(
        name=GET_ENTITY,
        description=_GET_DESCRIPTION,
        parameters={
            "type": "object",
            "properties": {
                "entity_id": {"type": "string", "minLength": 1, "description": "id позиции."}
            },
            "required": ["entity_id"],
            "additionalProperties": False,
        },
        handler=_get_handler(catalog),
        display_label="Смотрю карточку товара",
    )


def _filters_schema(attributes: Iterable[str]) -> dict[str, Any]:
    properties: dict[str, Any] = {
        "type": {"type": "string", "description": "Тип позиции, например product или service."},
        "category": {
            "type": "array",
            "items": {"type": "string", "minLength": 1},
            "minItems": 1,
            "description": "Любая из категорий.",
        },
        "price_min": {"type": "number", "minimum": 0},
        "price_max": {"type": "number", "minimum": 0},
        "in_stock": {"type": "boolean", "description": "true — только в наличии."},
    }
    names = list(dict.fromkeys(attributes))
    if names:
        condition = {
            "anyOf": [
                _SCALAR,
                {"type": "array", "items": _SCALAR, "minItems": 1},
                {
                    "type": "object",
                    "properties": {"min": {"type": "number"}, "max": {"type": "number"}},
                    "minProperties": 1,
                    "additionalProperties": False,
                },
            ]
        }
        properties["attributes"] = {
            "type": "object",
            "properties": dict.fromkeys(names, condition),
            "additionalProperties": False,
            "description": (
                "Характеристики: значение, список допустимых значений или {min, max} для чисел."
            ),
        }
    return {"type": "object", "properties": properties, "additionalProperties": False}


def _search_handler(catalog: Catalog) -> ToolHandler:
    async def handle(arguments: Mapping[str, Any], ctx: ToolContext, /) -> ToolResult:
        query = to_catalog_query(arguments)
        try:
            page = await catalog.search(ctx.tenant_id, query)
        except CatalogError as error:
            return _unavailable(SEARCH_CATALOG, ctx, error)
        content: dict[str, Any] = {
            "total": page.total,
            "items": [_entity_content(e) for e in page.items],
        }
        if not page.items:
            content["note"] = "в каталоге ничего не найдено — ослабь фильтры или измени query"
        return ToolResult(content=content)

    return handle


def _get_handler(catalog: Catalog) -> ToolHandler:
    async def handle(arguments: Mapping[str, Any], ctx: ToolContext, /) -> ToolResult:
        raw: str = arguments["entity_id"]
        try:
            entity_id = UUID(raw)
        except ValueError:
            return _not_found(raw)
        try:
            entity = await catalog.get(ctx.tenant_id, entity_id)
        except CatalogError as error:
            return _unavailable(GET_ENTITY, ctx, error)
        if entity is None:
            return _not_found(raw)
        return ToolResult(content=_entity_content(entity, full=True))

    return handle


def to_catalog_query(arguments: Mapping[str, Any]) -> CatalogQuery:
    """Аргументы уже прошли JSON Schema инструмента."""
    filters: Mapping[str, Any] = arguments.get("filters", {})
    return CatalogQuery(
        query=arguments.get("query"),
        filters=CatalogFilters(
            types=(filters["type"],) if "type" in filters else (),
            categories=tuple(filters.get("category", ())),
            price_min=_decimal(filters.get("price_min")),
            price_max=_decimal(filters.get("price_max")),
            in_stock=filters.get("in_stock"),
            attributes={
                name: _attribute_filter(value)
                for name, value in filters.get("attributes", {}).items()
            },
        ),
        sort=CatalogSort(arguments.get("sort", CatalogSort.RELEVANCE)),
        limit=arguments.get("limit", DEFAULT_LIMIT),
    )


def _attribute_filter(value: Any) -> AttributeFilter:
    if isinstance(value, dict):
        return AttributeFilter(min=_decimal(value.get("min")), max=_decimal(value.get("max")))
    return AttributeFilter(any_of=tuple(value) if isinstance(value, list) else (value,))


def _decimal(value: float | None) -> Decimal | None:
    # Через str: 0.1 → Decimal("0.1"), а не двоичное приближение float.
    return None if value is None else Decimal(str(value))


def _entity_content(entity: CatalogEntity, *, full: bool = False) -> dict[str, Any]:
    """Компактно: пустые поля опускаются, цена — числом."""
    content: dict[str, Any] = {"id": str(entity.id), "title": entity.title}
    if full:
        content["type"] = entity.type
    if entity.price is not None:
        content["price"] = _number(entity.price)
        if entity.currency:
            content["currency"] = entity.currency
    if entity.in_stock is not None:
        content["in_stock"] = entity.in_stock
    if entity.category:
        content["category"] = entity.category
    if entity.url:
        content["url"] = entity.url
    if full and entity.image_url:
        content["image_url"] = entity.image_url
    if entity.attributes:
        content["attributes"] = dict(entity.attributes)
    return content


def _number(value: Decimal) -> int | float:
    return int(value) if value == value.to_integral_value() else float(value)


def _not_found(raw: str) -> ToolResult:
    return ToolResult(
        error=ToolError(
            code="not_found",
            message=f"{GET_ENTITY}: позиции {raw!r} нет в каталоге — бери id из search_catalog",
            retryable=False,
        )
    )


def _unavailable(name: str, ctx: ToolContext, error: CatalogError) -> ToolResult:
    logger.warning(
        "catalog.request_failed",
        tool=name,
        tenant_id=str(ctx.tenant_id),
        conversation_id=str(ctx.conversation_id),
        turn_id=str(ctx.turn_id),
        error=str(error),
    )
    return ToolResult(
        error=ToolError(
            code="upstream_error", message=f"{name}: каталог временно недоступен", retryable=True
        )
    )
