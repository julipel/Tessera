"""Встроенный инструмент `show_entities` (contracts.md §3, §4, ADR-0004): модель передаёт id
позиций каталога, бэкенд собирает UI-компоненты из БД — `product_card`, `product_carousel`
или `comparison_table`. Цены, ссылки и характеристики берутся только из данных.

Строки таблицы сравнения — цена, наличие, категория и атрибуты из
`knowledge.catalog.attribute_labels` (в порядке конфига): ключи атрибутов пользователю не
показываются, подписи — бизнес-специфика тенанта.
"""

from collections.abc import Mapping, Sequence
from decimal import Decimal
from typing import Any
from uuid import UUID

import structlog

from app.contracts import (
    CatalogConfig,
    ComparisonTable,
    ComparisonTableRow,
    Price,
    ProductCard,
    ProductCarousel,
)
from app.modules.knowledge.kernel import CatalogEntity, CatalogError
from app.modules.tools.domain.definition import ToolContext, ToolDefinition, ToolHandler
from app.modules.tools.domain.ports import Catalog
from app.modules.tools.domain.result import ToolError, ToolResult

logger = structlog.get_logger(__name__)

SHOW_ENTITIES = "show_entities"
CARDS = "cards"
CAROUSEL = "carousel"
COMPARISON = "comparison"
IDS_MAX = 10
COMPARISON_MIN = 2
COMPARISON_MAX = 5
EMPTY = "—"

_DESCRIPTION = (
    "Покажи пользователю позиции каталога: передай id из search_catalog или get_entity, "
    "карточки с ценой, наличием и ссылкой бэкенд соберёт из каталога. layout: carousel — "
    "подборка из нескольких вариантов, cards — одна-две позиции отдельными карточками, "
    f"comparison — таблица сравнения {COMPARISON_MIN}..{COMPARISON_MAX} позиций. Порядок "
    "id — порядок показа."
)


def show_entities_tool(catalog: Catalog, settings: CatalogConfig | None = None) -> ToolDefinition:
    labels = dict((settings.attribute_labels if settings else None) or {})
    return ToolDefinition(
        name=SHOW_ENTITIES,
        description=_DESCRIPTION,
        parameters={
            "type": "object",
            "properties": {
                "entity_ids": {
                    "type": "array",
                    "items": {"type": "string", "minLength": 1},
                    "minItems": 1,
                    "maxItems": IDS_MAX,
                    "uniqueItems": True,
                    "description": "id позиций в порядке показа.",
                },
                "layout": {"type": "string", "enum": [CARDS, CAROUSEL, COMPARISON]},
                "title": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": 80,
                    "description": "Заголовок подборки (только для carousel).",
                },
            },
            "required": ["entity_ids", "layout"],
            "additionalProperties": False,
        },
        handler=_handler(catalog, labels),
        display_label="Показываю варианты",
    )


def _handler(catalog: Catalog, labels: Mapping[str, str]) -> ToolHandler:
    async def handle(arguments: Mapping[str, Any], ctx: ToolContext, /) -> ToolResult:
        raw_ids: list[str] = arguments["entity_ids"]
        layout: str = arguments["layout"]
        if layout == COMPARISON and not COMPARISON_MIN <= len(raw_ids) <= COMPARISON_MAX:
            return _invalid(f"для сравнения нужно {COMPARISON_MIN}..{COMPARISON_MAX} позиций")
        parsed = {raw: _uuid(raw) for raw in raw_ids}
        try:
            found = await catalog.get_many(
                ctx.tenant_id, [uid for uid in parsed.values() if uid is not None]
            )
        except CatalogError as error:
            return _unavailable(ctx, error)
        by_id = {entity.id: entity for entity in found}
        entities = [by_id[uid] for uid in parsed.values() if uid is not None and uid in by_id]
        missing = [raw for raw, uid in parsed.items() if uid is None or uid not in by_id]
        if not entities:
            return ToolResult(
                error=ToolError(
                    code="not_found",
                    message=(
                        f"{SHOW_ENTITIES}: позиций {missing!r} нет в каталоге — "
                        "бери id из search_catalog"
                    ),
                    retryable=False,
                )
            )
        if layout == COMPARISON and len(entities) < COMPARISON_MIN:
            return _invalid(
                f"в каталоге нашлась одна позиция из {len(raw_ids)}, сравнивать не с чем; "
                f"не найдены: {missing!r}"
            )
        content: dict[str, Any] = {
            "layout": layout,
            "shown": [{"id": str(e.id), "title": e.title} for e in entities],
        }
        if missing:
            content["not_found"] = missing
        return ToolResult(
            content=content,
            components=_components(layout, entities, labels, arguments.get("title")),
            state_patch={"shown_entities": [str(e.id) for e in entities]},
        )

    return handle


def _components(
    layout: str,
    entities: Sequence[CatalogEntity],
    labels: Mapping[str, str],
    title: str | None,
) -> tuple[dict[str, Any], ...]:
    if layout == COMPARISON:
        return (comparison_table(entities, labels),)
    cards = [product_card(entity) for entity in entities]
    if layout == CAROUSEL:
        carousel = ProductCarousel(type="product_carousel", title=title, items=cards)
        return (carousel.model_dump(mode="json"),)
    return tuple(card.model_dump(mode="json") for card in cards)


def product_card(entity: CatalogEntity) -> ProductCard:
    price = None
    if entity.price is not None and entity.currency:
        price = Price(amount=_number(entity.price), currency=entity.currency)
    return ProductCard(
        type="product_card",
        entity_id=str(entity.id),
        title=entity.title,
        subtitle=entity.category,
        image_url=entity.image_url,
        price=price,
        badges=[label] if (label := _stock_label(entity.in_stock)) else [],
        url=entity.url,
    )


def comparison_table(
    entities: Sequence[CatalogEntity], labels: Mapping[str, str]
) -> dict[str, Any]:
    """Строка, пустая у всех позиций, не выводится; пропуск у одной позиции — «—»."""
    rows: list[tuple[str, list[str | None]]] = [
        ("Цена", [_price_text(e) for e in entities]),
        ("Наличие", [_stock_label(e.in_stock) for e in entities]),
        ("Категория", [e.category for e in entities]),
    ]
    rows += [
        (label, [_value_text(e.attributes.get(key)) for e in entities])
        for key, label in labels.items()
    ]
    table = ComparisonTable(
        type="comparison_table",
        columns=[e.title for e in entities],
        rows=[
            ComparisonTableRow(label=label, values=[v or EMPTY for v in values])
            for label, values in rows
            if any(values)
        ],
    )
    return table.model_dump(mode="json")


def _stock_label(in_stock: bool | None) -> str | None:
    if in_stock is None:
        return None
    return "В наличии" if in_stock else "Нет в наличии"


def _price_text(entity: CatalogEntity) -> str | None:
    if entity.price is None:
        return None
    amount = _number_text(entity.price, money=True)
    return f"{amount} {entity.currency}" if entity.currency else amount


def _value_text(value: Any) -> str | None:
    if value is None or value == "" or value == []:
        return None
    if isinstance(value, bool):
        return "да" if value else "нет"
    if isinstance(value, list):
        return ", ".join(t for item in value if (t := _value_text(item)))
    if isinstance(value, int | float | Decimal):
        return _number_text(Decimal(str(value)))
    return str(value)


def _number(value: Decimal) -> int | float:
    return int(value) if value == value.to_integral_value() else float(value)


def _number_text(value: Decimal, *, money: bool = False) -> str:
    # Разряды через неразрывный пробел: 12990 → «12\u00a0990»; копейки — два знака.
    number = _number(value)
    spec = ",.2f" if money and isinstance(number, float) else ","
    return format(number, spec).replace(",", "\u00a0")


def _uuid(raw: str) -> UUID | None:
    try:
        return UUID(raw)
    except ValueError:
        return None


def _invalid(message: str) -> ToolResult:
    return ToolResult(
        error=ToolError(
            code="validation_error", message=f"{SHOW_ENTITIES}: {message}", retryable=False
        )
    )


def _unavailable(ctx: ToolContext, error: CatalogError) -> ToolResult:
    logger.warning(
        "catalog.request_failed",
        tool=SHOW_ENTITIES,
        tenant_id=str(ctx.tenant_id),
        conversation_id=str(ctx.conversation_id),
        turn_id=str(ctx.turn_id),
        error=str(error),
    )
    return ToolResult(
        error=ToolError(
            code="upstream_error",
            message=f"{SHOW_ENTITIES}: каталог временно недоступен",
            retryable=True,
        )
    )
