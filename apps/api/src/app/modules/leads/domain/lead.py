"""Заявка пользователя (create_lead, ADR-0021): значения полей формы тенанта."""

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from types import MappingProxyType
from uuid import UUID

from app.modules.shared.kernel import TenantId

MAX_PAGE = 100
# Номер заявки — INTEGER в Postgres: больший номер в запросе — 422, а не ошибка драйвера.
MAX_NUMBER = 2**31 - 1
# Потолок одной выгрузки CSV (ADR-0045): больше — просьба сузить период, а не обрезанный файл.
MAX_EXPORT = 10_000


@dataclass(frozen=True, slots=True, eq=True)
class Lead:
    """`form_key` — форма из `forms` конфига, по полям которой собрана заявка.
    `conversation_id` — None, если диалог уже удалён по сроку хранения (ADR-0043).
    `number` — номер заявки в тенанте (ADR-0045). `form_title` и `field_labels` — заголовок
    формы и подписи полей на момент отправки в порядке формы; у заявок до ADR-0045 — None
    и пусто. Хэша нет: поля — MappingProxyType."""

    __hash__ = None  # type: ignore[assignment]

    id: UUID
    tenant_id: TenantId
    conversation_id: UUID | None
    number: int
    form_key: str
    fields: Mapping[str, str]
    created_at: datetime
    form_title: str | None = None
    field_labels: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        # Копия: заявка не держит живой dict ORM-записи (REVIEW.md: L-12).
        object.__setattr__(self, "fields", MappingProxyType(dict(self.fields)))
        object.__setattr__(self, "field_labels", MappingProxyType(dict(self.field_labels)))

    def label(self, name: str) -> str:
        """Подпись поля; у старых заявок и неизвестных полей — ключ поля."""
        return self.field_labels.get(name, name)

    def ordered_fields(self) -> list[tuple[str, str, str]]:
        """`(name, label, value)`: сначала в порядке формы, затем поля без подписи (у старых
        заявок — все) в порядке хранения."""
        names = [n for n in self.field_labels if n in self.fields]
        names += [n for n in self.fields if n not in self.field_labels]
        return [(n, self.label(n), self.fields[n]) for n in names]


@dataclass(frozen=True, slots=True)
class LeadCursor:
    """Позиция в списке заявок от новых к старым: следующая страница — строго раньше."""

    created_at: datetime
    id: UUID


@dataclass(frozen=True, slots=True)
class LeadFilter:
    """`created_from` — включительно, `created_to` — не включая."""

    form_key: str | None = None
    number: int | None = None
    created_from: datetime | None = None
    created_to: datetime | None = None


@dataclass(frozen=True, slots=True)
class LeadPage:
    items: tuple[Lead, ...]
    next_cursor: LeadCursor | None
