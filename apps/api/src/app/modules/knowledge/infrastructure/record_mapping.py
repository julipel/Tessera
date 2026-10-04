"""Записи источника → элементы ingestion: общий разбор для `http_api` и `database`.

Запись — функция «ключ маппинга → значение» (путь в JSON, колонка результата запроса);
нескалярное значение функция отклоняет `ValueError`. Здесь — id записи, Entity или Document
по маппингу и повторы id. Ошибки записи — ошибки элементов: пустой id даёт ключ `#<номер>`,
повтор id — ошибку для всех записей с этим id (какая из них верная, неизвестно).
"""

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field

from pydantic import BaseModel, ConfigDict

from app.modules.knowledge.domain.ingestion import DocumentItem, RawItem
from app.modules.knowledge.infrastructure.entity_mapping import (
    AttributeSpec,
    EntityFields,
    Value,
    map_entity,
    text,
)

type Record = Callable[[str], Value]


class DocumentFields(BaseModel):
    """Поле Document → ключ в записи источника."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    external_id: str
    title: str
    text: str
    url: str | None = None


@dataclass(frozen=True, slots=True)
class EntityMapping:
    entity_type: str
    fields: EntityFields
    attributes: Mapping[str, AttributeSpec] = field(default_factory=dict)
    currency: str | None = None  # если поля валюты нет или оно пустое


type ItemMapping = EntityMapping | DocumentFields


@dataclass(frozen=True, slots=True)
class MappedRecord:
    """Элемент записи или ошибка, которую `fetch` отдаст как ошибку элемента."""

    item: RawItem | None
    error: str | None = None


def map_records(records: Iterable[Record], mapping: ItemMapping) -> dict[str, MappedRecord]:
    """external_id → элемент или ошибка; порядок — порядок записей."""
    id_key = (
        mapping.fields.external_id if isinstance(mapping, EntityMapping) else mapping.external_id
    )
    result: dict[str, MappedRecord] = {}
    first: dict[str, int] = {}
    for number, record in enumerate(records, start=1):
        where = f"запись {number}"
        try:
            external_id = text(record(id_key))
            if external_id is None:
                raise ValueError(f"пустой id («{id_key}»)")
        except ValueError as e:
            result[f"#{number}"] = MappedRecord(None, f"{where}: {e}")
            continue
        try:
            entry = MappedRecord(map_record(record, mapping, external_id))
        except ValueError as e:
            entry = MappedRecord(None, f"{where}: {e}")
        if external_id in result:
            error = f"id «{external_id}» повторяется: записи {first[external_id]}, {number}"
            result[external_id] = MappedRecord(None, error)
            continue
        result[external_id] = entry
        first[external_id] = number
    return result


def map_record(record: Record, mapping: ItemMapping, external_id: str) -> RawItem:
    if isinstance(mapping, EntityMapping):
        return map_entity(
            record,
            entity_type=mapping.entity_type,
            external_id=external_id,
            fields=mapping.fields,
            attributes=mapping.attributes,
            currency=mapping.currency,
        )
    title = text(record(mapping.title))
    if title is None:
        raise ValueError(f"пустое название («{mapping.title}»)")
    body = text(record(mapping.text))
    if body is None:
        raise ValueError(f"пустой текст («{mapping.text}»)")
    url = text(record(mapping.url)) if mapping.url else None
    return DocumentItem(external_id=external_id, title=title, text=body, url=url)
