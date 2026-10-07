"""Инструмент show_entities (P5-01): компоненты product_card / product_carousel /
comparison_table из данных каталога, порядок показа, не найденные id, ошибки, state_patch."""

import uuid
from collections.abc import Sequence
from decimal import Decimal
from typing import Any

import pytest

from app.contracts import CatalogConfig, Component
from app.modules.knowledge.public import CatalogEntity
from app.modules.shared.kernel import TenantId
from app.modules.tools.public import ToolDefinition, ToolResult, show_entities_tool
from test_catalog_tools import CREAM, SERUM, TENANT, FakeCatalog, call

MASK = CatalogEntity(
    id=uuid.UUID("00000000-0000-0000-0000-000000000003"),
    type="product",
    title="Маска на заказ",
    in_stock=False,
    attributes={"skin_type": ["сухая", "нормальная"], "fragrance_free": True},
)
LABELS = {"skin_type": "Тип кожи", "fragrance_free": "Без отдушек", "spf": "SPF"}


def tool(
    catalog: FakeCatalog | None = None, settings: CatalogConfig | None = None
) -> ToolDefinition:
    return show_entities_tool(catalog or FakeCatalog([CREAM, SERUM, MASK]), settings)


async def show(
    ids: Sequence[uuid.UUID | str], layout: str, catalog: FakeCatalog | None = None, **extra: Any
) -> ToolResult:
    labels = extra.pop("labels", None)
    settings = extra.pop("settings", None)
    if labels is not None:
        settings = CatalogConfig(attribute_labels=labels)
    arguments = {"entity_ids": [str(i) for i in ids], "layout": layout, **extra}
    return await call(tool(catalog, settings), arguments)


def validated(result: ToolResult) -> list[dict[str, Any]]:
    """Компоненты проходят схему контракта (contracts.md §3)."""
    for component in result.components:
        Component.model_validate(component)
    return list(result.components)


async def test_cards_from_catalog_data_in_requested_order() -> None:
    catalog = FakeCatalog([CREAM, SERUM, MASK])

    result = await show([MASK.id, CREAM.id], "cards", catalog)

    mask, cream = validated(result)
    assert cream == {
        "type": "product_card",
        "entity_id": str(CREAM.id),
        "title": "Крем для сухой кожи",
        "subtitle": "Уход за лицом",
        "image_url": "https://shop.example/cream.jpg",
        "price": {"amount": 1990, "currency": "RUB"},
        "badges": ["В наличии"],
        "url": "https://shop.example/cream",
        "actions": [],
    }
    # Без цены — price null, а не выдуманная цена; наличие «нет» — свой бейдж.
    assert mask["price"] is None and mask["badges"] == ["Нет в наличии"]
    assert result.content == {
        "layout": "cards",
        "shown": [
            {"id": str(MASK.id), "title": "Маска на заказ"},
            {"id": str(CREAM.id), "title": "Крем для сухой кожи"},
        ],
    }
    assert result.state_patch == {"shown_entities": [str(MASK.id), str(CREAM.id)]}
    assert catalog.batches == [(TENANT, [MASK.id, CREAM.id])]


async def test_price_without_currency_is_not_shown_in_card() -> None:
    # Валюта нужна Price по контракту; SERUM без валюты.
    [card] = validated(await show([SERUM.id], "cards"))

    assert card["price"] is None
    assert card["badges"] == [] and card["subtitle"] is None


async def test_carousel_with_title() -> None:
    result = await show([SERUM.id, CREAM.id], "carousel", title="Подходящие варианты")

    [carousel] = validated(result)
    assert carousel["type"] == "product_carousel"
    assert carousel["title"] == "Подходящие варианты"
    assert [item["entity_id"] for item in carousel["items"]] == [str(SERUM.id), str(CREAM.id)]


CARD_ACTIONS = CatalogConfig.model_validate(
    {
        "card_actions": [
            {"action_id": "ask_about", "label": "Подробнее"},
            {"action_id": "select_product", "label": "Выбрать", "style": "primary"},
        ]
    }
)


async def test_card_actions_from_config_carry_entity_id() -> None:
    [carousel] = validated(await show([SERUM.id, CREAM.id], "carousel", settings=CARD_ACTIONS))

    serum, cream = carousel["items"]
    assert serum["actions"] == [
        {
            "action_id": "ask_about",
            "label": "Подробнее",
            "style": "secondary",
            "payload": {"entity_id": str(SERUM.id)},
            "url": None,
        },
        {
            "action_id": "select_product",
            "label": "Выбрать",
            "style": "primary",
            "payload": {"entity_id": str(SERUM.id)},
            "url": None,
        },
    ]
    assert [a["payload"] for a in cream["actions"]] == [{"entity_id": str(CREAM.id)}] * 2


async def test_comparison_has_no_card_actions() -> None:
    [table] = validated(await show([SERUM.id, CREAM.id], "comparison", settings=CARD_ACTIONS))

    assert "actions" not in table


async def test_comparison_rows_from_labels_in_config_order() -> None:
    result = await show([CREAM.id, MASK.id, SERUM.id], "comparison", labels=LABELS)

    [table] = validated(result)
    assert table["columns"] == ["Крем для сухой кожи", "Маска на заказ", "Сыворотка"]
    assert table["rows"] == [
        {"label": "Цена", "values": ["1\u00a0990 RUB", "—", "2\u00a0590.50"]},
        {"label": "Наличие", "values": ["В наличии", "Нет в наличии", "—"]},
        {"label": "Категория", "values": ["Уход за лицом", "—", "—"]},
        {"label": "Тип кожи", "values": ["сухая", "сухая, нормальная", "—"]},
        {"label": "Без отдушек", "values": ["—", "да", "—"]},
        # spf нет ни у одной позиции — строки нет; volume_ml без подписи не выводится.
    ]


async def test_comparison_without_labels_shows_only_core_fields() -> None:
    [table] = validated(await show([CREAM.id, SERUM.id], "comparison"))

    assert [row["label"] for row in table["rows"]] == ["Цена", "Наличие", "Категория"]


@pytest.mark.parametrize("count", [1, 6])
async def test_comparison_needs_two_to_five_ids(count: int) -> None:
    ids = [CREAM.id, *(uuid.uuid4() for _ in range(count - 1))]
    catalog = FakeCatalog([CREAM])

    result = await show(ids, "comparison", catalog)

    assert result.error is not None and result.error.code == "validation_error"
    assert catalog.batches == []


async def test_comparison_with_one_found_is_validation_error() -> None:
    missing = uuid.uuid4()

    result = await show([CREAM.id, missing], "comparison")

    assert result.error is not None and result.error.code == "validation_error"
    assert str(missing) in result.error.message


async def test_partially_found_ids_are_reported_to_model() -> None:
    missing = uuid.uuid4()

    result = await show([missing, CREAM.id, "sku-1"], "carousel")

    [carousel] = validated(result)
    assert [item["entity_id"] for item in carousel["items"]] == [str(CREAM.id)]
    assert isinstance(result.content, dict)
    assert result.content["not_found"] == [str(missing), "sku-1"]
    assert result.state_patch == {"shown_entities": [str(CREAM.id)]}


async def test_nothing_found_is_not_found_error() -> None:
    catalog = FakeCatalog([CREAM])

    result = await show([uuid.uuid4(), "Крем"], "cards", catalog)

    assert result.error is not None
    assert result.error.code == "not_found" and not result.error.retryable
    assert "search_catalog" in result.error.message
    assert result.components == () and result.state_patch is None


async def test_other_tenant_entities_are_not_shown() -> None:
    # Каталог возвращает только сущности тенанта из ToolContext.
    catalog = FakeCatalog([CREAM])
    other = TenantId(uuid.uuid4())

    await call(tool(catalog), {"entity_ids": [str(CREAM.id)], "layout": "cards"}, other)

    assert [tenant_id for tenant_id, _ in catalog.batches] == [other]


async def test_catalog_failure_is_retryable_upstream_error() -> None:
    result = await show([CREAM.id], "cards", FakeCatalog(fail=True))

    assert result.error is not None
    assert result.error.code == "upstream_error" and result.error.retryable


@pytest.mark.parametrize(
    "arguments",
    [
        {"entity_ids": [], "layout": "cards"},
        {"entity_ids": ["a", "a"], "layout": "cards"},
        {"entity_ids": [str(uuid.uuid4()) for _ in range(11)], "layout": "carousel"},
        {"entity_ids": ["a"], "layout": "grid"},
        {"entity_ids": ["a"], "layout": "cards", "price": 1},
    ],
)
async def test_invalid_arguments_are_rejected_by_schema(arguments: dict[str, Any]) -> None:
    catalog = FakeCatalog()

    result = await call(tool(catalog), arguments)

    assert result.error is not None and result.error.code == "validation_error"
    assert catalog.batches == []


async def test_number_formatting_in_comparison() -> None:
    big = CatalogEntity(
        id=uuid.uuid4(),
        type="product",
        title="X",
        price=Decimal("12990"),
        currency="RUB",
        attributes={"volume_ml": Decimal("50.0"), "spf": 30.5},
    )
    catalog = FakeCatalog([big, CREAM])

    result = await show(
        [big.id, CREAM.id], "comparison", catalog, labels={"volume_ml": "Объём", "spf": "SPF"}
    )

    [table] = validated(result)
    values = {row["label"]: row["values"] for row in table["rows"]}
    assert values["Цена"] == ["12\u00a0990 RUB", "1\u00a0990 RUB"]
    assert values["Объём"] == ["50", "50"] and values["SPF"] == ["30.5", "—"]


@pytest.mark.parametrize(
    ("language", "badges", "rows", "yes"),
    [
        ("en", ("In stock", "Out of stock"), ["Price", "Availability", "Category"], "yes"),
        ("sv", ("I lager", "Slut i lager"), ["Pris", "Tillgänglighet", "Kategori"], "ja"),
    ],
)
async def test_platform_strings_in_conversation_language(
    language: str, badges: tuple[str, str], rows: list[str], yes: str
) -> None:
    """Строки платформы — на языке диалога (P6-04d, ADR-0025); подписи тенанта и данные
    каталога не переводятся."""
    definition = show_entities_tool(
        FakeCatalog([CREAM, SERUM, MASK]), CatalogConfig(attribute_labels=LABELS), language
    )

    cream, mask = validated(
        await call(definition, {"entity_ids": [str(CREAM.id), str(MASK.id)], "layout": "cards"})
    )
    [table] = validated(
        await call(
            definition, {"entity_ids": [str(CREAM.id), str(MASK.id)], "layout": "comparison"}
        )
    )

    assert (cream["badges"], mask["badges"]) == ([badges[0]], [badges[1]])
    assert [row["label"] for row in table["rows"]] == [*rows, "Тип кожи", "Без отдушек"]
    assert table["rows"][1]["values"] == list(badges)
    assert table["rows"][-1]["values"] == ["—", yes]
