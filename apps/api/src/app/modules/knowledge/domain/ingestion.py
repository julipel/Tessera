"""Элементы ingestion: что коннектор отдаёт пайплайну и что пайплайн записывает (§8)."""

import hashlib
import json
from dataclasses import asdict, dataclass, field
from decimal import Decimal
from enum import StrEnum
from typing import Any
from uuid import UUID

from app.modules.knowledge.domain.entities import SourceKind, SyncStatus


@dataclass(frozen=True, slots=True)
class RawItemRef:
    """Ссылка на элемент источника. `version` — подсказка коннектора (ETag, mtime), не хэш."""

    external_id: str
    version: str | None = None


@dataclass(frozen=True, slots=True)
class Listing:
    """Результат discover/changed_since; `cursor` — курсор для следующего changed_since."""

    refs: list[RawItemRef]
    cursor: str | None = None


@dataclass(frozen=True, slots=True)
class DocumentItem:
    """Текстовый документ, уже нормализованный коннектором."""

    external_id: str
    title: str
    text: str
    url: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class EntityItem:
    """Структурированная запись, поля уже отображены по маппингу из конфига источника."""

    external_id: str
    type: str
    title: str
    price: Decimal | None = None
    currency: str | None = None
    in_stock: bool | None = None
    category: str | None = None
    url: str | None = None
    image_url: str | None = None
    attributes: dict[str, Any] = field(default_factory=dict)


type RawItem = DocumentItem | EntityItem


class ItemKind(StrEnum):
    DOCUMENT = "document"
    ENTITY = "entity"


@dataclass(frozen=True, slots=True, order=True)
class ItemKey:
    """external_id уникален в пределах источника отдельно для документов и сущностей."""

    kind: ItemKind
    external_id: str


def item_key(item: RawItem) -> ItemKey:
    kind = ItemKind.DOCUMENT if isinstance(item, DocumentItem) else ItemKind.ENTITY
    return ItemKey(kind, item.external_id)


def content_hash(item: RawItem) -> str:
    """sha256 канонического JSON элемента: не зависит от порядка ключей и от коннектора."""
    payload = {"kind": item_key(item).kind, **asdict(item)}
    canonical = json.dumps(
        payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"), default=str
    )
    return hashlib.sha256(canonical.encode()).hexdigest()


@dataclass(frozen=True, slots=True)
class ChunkDraft:
    ord: int
    text: str
    token_count: int
    section: str | None = None


@dataclass(frozen=True, slots=True)
class SyncJob:
    """Синхронизация вместе с конфигом её источника."""

    sync_id: UUID
    source_id: UUID
    status: SyncStatus
    kind: SourceKind
    config: dict[str, Any]


MAX_STORED_ERRORS = 20


@dataclass(slots=True)
class SyncStats:
    """Счётчики синхронизации; в `SourceSync.stats` пишется `as_dict()`."""

    discovered: int = 0
    created: int = 0
    updated: int = 0
    unchanged: int = 0
    deleted: int = 0
    failed: int = 0
    incremental: bool = False
    errors: list[str] = field(default_factory=list)

    def add_error(self, external_id: str, error: BaseException) -> None:
        self.failed += 1
        if len(self.errors) < MAX_STORED_ERRORS:
            self.errors.append(f"{external_id}: {type(error).__name__}: {error}")

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)
