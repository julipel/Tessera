"""Строки платформы, которые видит посетитель, на языке диалога (ADR-0025).

Русские подписи — в определениях инструментов и в `ConfirmLabels`; здесь — переводы.
Подписи без перевода остаются русскими; полноту переводов встроенных инструментов
проверяет тест. Строки компонентов каталога (`CatalogTexts`) — здесь на всех языках.
"""

from collections.abc import Iterable
from dataclasses import dataclass, replace

from app.modules.tools.application.catalog import GET_ENTITY, SEARCH_CATALOG
from app.modules.tools.application.forms import CREATE_LEAD
from app.modules.tools.application.knowledge import SEARCH_KNOWLEDGE
from app.modules.tools.domain.definition import ConfirmLabels, ToolDefinition

# builtin.py и show_entities.py импортируют этот модуль, поэтому их имена — строками.
_UPDATE_DIALOG_STATE = "update_dialog_state"
_SHOW_ENTITIES = "show_entities"

_DISPLAY_LABELS: dict[str, dict[str, str]] = {
    "en": {
        SEARCH_CATALOG: "Searching the catalog",
        GET_ENTITY: "Viewing product details",
        _SHOW_ENTITIES: "Showing options",
        SEARCH_KNOWLEDGE: "Searching the knowledge base",
        _UPDATE_DIALOG_STATE: "Noting that",
        CREATE_LEAD: "Preparing your request",
    },
    "sv": {
        SEARCH_CATALOG: "Söker i katalogen",
        GET_ENTITY: "Tittar på produkten",
        _SHOW_ENTITIES: "Visar alternativ",
        SEARCH_KNOWLEDGE: "Söker i kunskapsbasen",
        _UPDATE_DIALOG_STATE: "Antecknar",
        CREATE_LEAD: "Förbereder din förfrågan",
    },
}

_CONFIRM_LABELS: dict[str, ConfirmLabels] = {
    "en": ConfirmLabels(confirm="Confirm", cancel="Cancel"),
    "sv": ConfirmLabels(confirm="Bekräfta", cancel="Avbryt"),
}


def localize_display_labels(
    definitions: Iterable[ToolDefinition], language: str
) -> list[ToolDefinition]:
    """Подписи tool_started на языке диалога; у инструмента без подписи её не появляется."""
    labels = _DISPLAY_LABELS.get(language, {})
    return [
        replace(d, display_label=labels[d.name])
        if d.display_label is not None and d.name in labels
        else d
        for d in definitions
    ]


def confirm_labels(
    language: str, confirm: str | None = None, cancel: str | None = None
) -> ConfirmLabels:
    """Подписи кнопок confirm: заданные тенантом (`confirm`/`cancel`), остальные — платформы
    на языке диалога."""
    default = _CONFIRM_LABELS.get(language, ConfirmLabels())
    return ConfirmLabels(confirm=confirm or default.confirm, cancel=cancel or default.cancel)


@dataclass(frozen=True, slots=True)
class CatalogTexts:
    """Строки платформы в компонентах `show_entities`: бейдж наличия в карточке, строки
    таблицы сравнения и значения логических атрибутов."""

    in_stock: str
    out_of_stock: str
    price: str
    availability: str
    category: str
    yes: str
    no: str


_CATALOG_TEXTS: dict[str, CatalogTexts] = {
    "ru": CatalogTexts(
        in_stock="В наличии",
        out_of_stock="Нет в наличии",
        price="Цена",
        availability="Наличие",
        category="Категория",
        yes="да",
        no="нет",
    ),
    "en": CatalogTexts(
        in_stock="In stock",
        out_of_stock="Out of stock",
        price="Price",
        availability="Availability",
        category="Category",
        yes="yes",
        no="no",
    ),
    "sv": CatalogTexts(
        in_stock="I lager",
        out_of_stock="Slut i lager",
        price="Pris",
        availability="Tillgänglighet",
        category="Kategori",
        yes="ja",
        no="nej",
    ),
}


def catalog_texts(language: str) -> CatalogTexts:
    return _CATALOG_TEXTS.get(language, _CATALOG_TEXTS["ru"])
