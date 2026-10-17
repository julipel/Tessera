"""Заявки в админке (ADR-0045): список страницами и выгрузка CSV."""

import base64
import binascii
import csv
import io
from dataclasses import replace
from datetime import UTC, datetime
from uuid import UUID

from app.modules.leads.domain.errors import (
    InvalidCursorError,
    InvalidPeriodError,
    TooManyLeadsError,
)
from app.modules.leads.domain.lead import (
    MAX_EXPORT,
    MAX_PAGE,
    Lead,
    LeadCursor,
    LeadFilter,
    LeadPage,
)
from app.modules.leads.domain.ports import LeadReader
from app.modules.shared.kernel import TenantId

# Excel открывает формулой значение с такого символа — CSV-инъекция (ADR-0045 п.4).
# Время, которое Excel распознаёт как дату (ISO с T и поясом остаётся текстом).
_EXCEL_TIME = "%Y-%m-%d %H:%M:%S"
_FORMULA_START = ("=", "+", "-", "@", "\t", "\r", "\n")


def encode_cursor(cursor: LeadCursor) -> str:
    raw = f"{cursor.created_at.isoformat()}|{cursor.id}"
    return base64.urlsafe_b64encode(raw.encode()).decode().rstrip("=")


def decode_cursor(value: str) -> LeadCursor:
    try:
        raw = base64.urlsafe_b64decode(value + "=" * (-len(value) % 4)).decode()
        created_at, _, id_ = raw.partition("|")
        moment = datetime.fromisoformat(created_at)
        if moment.tzinfo is None:
            raise ValueError("время курсора без пояса")
        # В UTC: на краю календаря (0001-01-01+14:00) перевод переполняется — это не 500.
        return LeadCursor(moment.astimezone(UTC), UUID(id_))
    except (binascii.Error, UnicodeDecodeError, ValueError, OverflowError) as e:
        raise InvalidCursorError("неверный курсор страницы") from e


async def list_leads(
    tenant_id: TenantId,
    reader: LeadReader,
    *,
    before: str | None,
    limit: int,
    where: LeadFilter,
) -> LeadPage:
    """Страница от новых к старым; лишняя строка показывает, есть ли следующая."""
    limit = max(1, min(limit, MAX_PAGE))
    items = await reader.list_page(
        tenant_id,
        before=decode_cursor(before) if before else None,
        limit=limit + 1,
        where=where,
    )
    page, rest = items[:limit], items[limit:]
    last = page[-1] if rest else None
    return LeadPage(tuple(page), LeadCursor(last.created_at, last.id) if last else None)


async def export_leads(tenant_id: TenantId, reader: LeadReader, where: LeadFilter) -> list[Lead]:
    """Заявки выгрузки от новых к старым. Больше MAX_EXPORT — ошибка, а не обрезанный файл."""
    try:
        where = replace(
            where,
            created_from=_utc(where.created_from),
            created_to=_utc(where.created_to),
        )
    except OverflowError as e:
        raise InvalidPeriodError("время периода вне календаря") from e
    if (
        where.created_from is not None
        and where.created_to is not None
        and where.created_from >= where.created_to
    ):
        raise InvalidPeriodError("начало периода должно быть раньше конца")
    leads = await reader.list_page(tenant_id, before=None, limit=MAX_EXPORT + 1, where=where)
    if len(leads) > MAX_EXPORT:
        raise TooManyLeadsError(
            f"в выгрузке больше {MAX_EXPORT} заявок — сузьте период или выберите форму"
        )
    return leads


def leads_csv(leads: list[Lead]) -> str:
    """CSV для Excel (ADR-0045 п.4): BOM, `;`, CRLF; колонки полей — в порядке первого
    появления (внутри заявки — порядок формы), заголовок — подпись из первой заявки с этим
    полем."""
    columns: dict[str, str] = {}
    for lead in leads:
        for name, label, _ in lead.ordered_fields():
            columns.setdefault(name, label)
    out = io.StringIO()
    out.write("\ufeff")
    writer = csv.writer(out, delimiter=";", lineterminator="\r\n")
    writer.writerow(
        ["Номер", "Создана (UTC)", "Форма", "Диалог", *(_safe(c) for c in columns.values())]
    )
    for lead in leads:
        writer.writerow(
            [
                lead.number,
                lead.created_at.astimezone(UTC).strftime(_EXCEL_TIME),
                _safe(lead.form_title or lead.form_key),
                lead.conversation_id or "",
                *(_safe(lead.fields.get(name, "")) for name in columns),
            ]
        )
    return out.getvalue()


def _utc(moment: datetime | None) -> datetime | None:
    return moment.astimezone(UTC) if moment is not None else None


def _safe(value: str) -> str:
    return f"'{value}" if value.startswith(_FORMULA_START) else value
