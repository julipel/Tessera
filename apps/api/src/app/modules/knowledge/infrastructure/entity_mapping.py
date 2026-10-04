"""Маппинг записи источника в Entity: общий для коннекторов `table` и `http_api` (ADR-0003).

Коннектор отдаёт функцию «ключ маппинга → значение» (колонка таблицы, путь в JSON), здесь —
разбор значений: цена, валюта, наличие, типизированные атрибуты. Неразбираемое значение —
`ValueError` с понятным текстом; коннектор превращает его в ошибку элемента.

Атрибут `list` — строка с разделителем («dry, oily») или массив источника (JSON, массив
Postgres); остальные поля и атрибуты принимают только скаляры.
"""

import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import date, datetime, time
from decimal import Decimal, InvalidOperation
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.modules.knowledge.domain.ingestion import EntityItem

type Scalar = str | int | float | bool | Decimal | datetime | date | time
type Value = Scalar | list[Scalar] | None
type AttributeType = Literal["string", "number", "boolean", "list"]
type AttributeResult = str | int | float | bool | list[str | int | float | bool]

DEFAULT_LIST_SEPARATOR = ","


class EntityFields(BaseModel):
    """Нормализованное поле Entity → ключ в записи источника (колонка, путь)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    external_id: str
    title: str
    price: str | None = None
    currency: str | None = None
    in_stock: str | None = None
    category: str | None = None
    url: str | None = None
    image_url: str | None = None


@dataclass(frozen=True, slots=True)
class AttributeSpec:
    key: str
    type: AttributeType = "string"
    separator: str = DEFAULT_LIST_SEPARATOR  # только для `list` из строки


class AttributeColumn(BaseModel):
    """Атрибут из колонки (`table`, `database`): `{column, type, separator}`."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    column: str
    type: AttributeType = "string"
    separator: str = Field(default=DEFAULT_LIST_SEPARATOR, min_length=1)

    def spec(self) -> AttributeSpec:
        return AttributeSpec(self.column, self.type, self.separator)


def map_entity(
    value: Callable[[str], Value],
    *,
    entity_type: str,
    external_id: str,
    fields: EntityFields,
    attributes: Mapping[str, AttributeSpec],
    currency: str | None = None,
) -> EntityItem:
    """Entity из записи; `currency` — если поля валюты нет или оно пустое."""

    def get(key: str | None) -> Value:
        return None if key is None else value(key)

    title = text(get(fields.title))
    if title is None:
        raise ValueError(f"пустое название («{fields.title}»)")
    return EntityItem(
        external_id=external_id,
        type=entity_type,
        title=title,
        price=price(get(fields.price)),
        currency=currency_code(text(get(fields.currency)) or currency),
        in_stock=boolean(get(fields.in_stock)),
        category=text(get(fields.category)),
        url=text(get(fields.url)),
        image_url=text(get(fields.image_url)),
        attributes={
            name: parsed
            for name, spec in attributes.items()
            if (parsed := attribute(get(spec.key), spec.type, spec.separator)) is not None
        },
    )


_TRUE = {"да", "true", "yes", "y", "+", "есть", "в наличии", "имеется"}
_FALSE = {"нет", "false", "no", "n", "-", "\u2212", "—"}
_FALSE |= {"нет в наличии", "отсутствует", "под заказ"}
_NOT_NUMBER = re.compile(r"[^\d.,\-]")


def text(value: Value) -> str | None:
    if value is None:
        return None
    if isinstance(value, list):
        raise ValueError("список, а нужно одно значение")
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, float) and value.is_integer():
        return str(int(value))  # артикул 12345 из XLSX приходит как 12345.0
    if isinstance(value, datetime | date | time):
        return value.isoformat()
    result = str(value).strip()
    return result or None


def decimal(value: Value) -> Decimal | None:
    """`1 990,50 ₽`, `1,990.50`, `1990.5` → Decimal. Одиночная запятая — десятичная.
    Текст без цифр («по запросу», «договорная») — значение неизвестно (None)."""
    if isinstance(value, bool | datetime | date | time | list):
        raise ValueError(f"не число: {value}")
    if isinstance(value, int | float | Decimal):
        number = Decimal(str(value))
        if not number.is_finite():
            raise ValueError(f"не число: {value}")
        return number
    raw_text = text(value)
    if raw_text is None:
        return None
    raw = _NOT_NUMBER.sub("", raw_text).strip(".,")  # «1990 р.»
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
        raise ValueError(f"не число: «{raw_text}»") from None
    if not number.is_finite():
        raise ValueError(f"не число: «{raw_text}»")
    return number


def currency_code(code: str | None) -> str | None:
    if code is None:
        return None
    if not re.fullmatch(r"[A-Za-z]{3}", code):
        raise ValueError(f"валюта: «{code}» — не код ISO 4217 (RUB, EUR…)")
    return code.upper()


def price(value: Value) -> Decimal | None:
    try:
        number = decimal(value)
    except ValueError as e:
        raise ValueError(f"цена: {e}") from None
    if number is not None and number < 0:
        raise ValueError(f"цена: отрицательная ({number})")
    return number


def boolean(value: Value) -> bool | None:
    """Да/нет, true/false, +/-, «в наличии»; число — остаток: больше нуля → True."""
    if value is None or isinstance(value, bool):
        return value
    raw_text = text(value)
    if raw_text is None:
        return None
    if raw_text.lower() in _TRUE:
        return True
    if raw_text.lower() in _FALSE:
        return False
    try:
        number = decimal(value)
    except ValueError:
        number = None
    if number is None:
        raise ValueError(f"не да/нет: «{raw_text}»")
    return number > 0


def attribute(
    value: Value, kind: AttributeType, separator: str = DEFAULT_LIST_SEPARATOR
) -> AttributeResult | None:
    if kind == "boolean":
        return boolean(value)
    if kind == "number":
        number = decimal(value)
        return None if number is None else _number(number)
    if kind == "list":
        return value_list(value, separator)
    return text(value)


def value_list(
    value: Value, separator: str = DEFAULT_LIST_SEPARATOR
) -> list[str | int | float | bool] | None:
    """Массив источника или строка через `separator` → список без пустых элементов и
    повторов (порядок сохраняется); пустой список — значения нет (None)."""
    if isinstance(value, list):
        elements: list[Scalar] = value
    else:
        raw = text(value)
        if raw is None:
            return None
        elements = list(raw.split(separator))
    result: list[str | int | float | bool] = []
    seen: set[tuple[type, object]] = set()
    for element in elements:
        item = _list_element(element)
        # По типу и значению: True и 1 — разные элементы.
        if item is not None and (type(item), item) not in seen:
            seen.add((type(item), item))
            result.append(item)
    return result or None


def _list_element(element: Scalar | None) -> str | int | float | bool | None:
    if element is None or isinstance(element, bool):
        return element
    if isinstance(element, int | float | Decimal):
        number = decimal(element)
        return None if number is None else _number(number)
    return text(element)


def _number(number: Decimal) -> int | float:
    return int(number) if number == number.to_integral_value() else float(number)
