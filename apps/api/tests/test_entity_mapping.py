"""Разбор значений маппинга Entity: атрибут `list` и отказ скалярных полей от массивов."""

from datetime import date
from decimal import Decimal
from typing import Any

import pytest

from app.modules.knowledge.infrastructure.entity_mapping import (
    AttributeSpec,
    EntityFields,
    Value,
    attribute,
    map_entity,
    text,
    value_list,
)


@pytest.mark.parametrize(
    ("value", "separator", "expected"),
    [
        ("dry, oily ,sensitive", ",", ["dry", "oily", "sensitive"]),
        # Пустые элементы и повторы отбрасываются, порядок сохраняется.
        ("dry,, oily, dry,", ",", ["dry", "oily"]),
        ("ниацинамид | пантенол", "|", ["ниацинамид", "пантенол"]),
        # Без разделителя в строке — список из одного элемента.
        ("dry", ",", ["dry"]),
        # Массив источника: числа нормализуются, True и 1 — разные элементы.
        (["a", " b ", "", None, "a"], ",", ["a", "b"]),
        ([Decimal("50"), 50, 2.5, True, 1], ",", [50, 2.5, True, 1]),
        ([date(2026, 1, 2)], ",", ["2026-01-02"]),
        # Значения нет.
        (None, ",", None),
        ("  ", ",", None),
        (" , ,", ",", None),
        ([], ",", None),
    ],
)
def test_value_list(value: Value, separator: str, expected: list[Any] | None) -> None:
    assert value_list(value, separator) == expected


def test_list_attribute_of_number_cell() -> None:
    # XLSX отдаёт число — список из одного элемента-строки, как и текст ячейки.
    assert attribute(5.0, "list") == ["5"]


@pytest.mark.parametrize("kind", ["string", "number", "boolean"])
def test_scalar_attribute_rejects_list(kind: Any) -> None:
    with pytest.raises(ValueError, match=r"список|не число"):
        attribute(["a"], kind)


def test_scalar_field_rejects_list() -> None:
    with pytest.raises(ValueError, match="список"):
        text(["Крем"])


def test_map_entity_with_list_attribute() -> None:
    record: dict[str, Value] = {"id": "A-1", "name": "Крем", "skin": "dry; oily", "tags": None}

    item = map_entity(
        record.__getitem__,
        entity_type="product",
        external_id="A-1",
        fields=EntityFields(external_id="id", title="name"),
        attributes={
            "skin_types": AttributeSpec("skin", "list", ";"),
            "tags": AttributeSpec("tags", "list"),
        },
    )

    assert item.attributes == {"skin_types": ["dry", "oily"]}
