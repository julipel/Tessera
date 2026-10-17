"""API админки: заявки тенанта, список и выгрузка CSV (docs/contracts.md §7, ADR-0045).
Доступ — роль viewer в тенанте."""

from datetime import datetime
from typing import Annotated
from uuid import UUID

import structlog
from fastapi import APIRouter, Query, Response, status

from app.contracts import LeadFieldValue, LeadList, LeadSummary
from app.modules.leads.application.admin import (
    encode_cursor,
    export_leads,
    leads_csv,
    list_leads,
)
from app.modules.leads.domain.errors import (
    InvalidCursorError,
    InvalidPeriodError,
    TooManyLeadsError,
)
from app.modules.leads.domain.lead import MAX_NUMBER, MAX_PAGE, Lead, LeadFilter
from app.modules.leads.infrastructure.repositories import LeadRepository
from app.modules.shared.public import AdminViewer, ApiError, DbSession, TenantId

router = APIRouter(prefix="/v1/admin/tenants/{tenant_id}", tags=["admin"])
logger = structlog.get_logger(__name__)

FormKey = Annotated[str | None, Query(max_length=64)]


@router.get("/leads", dependencies=[AdminViewer])
async def leads(
    tenant_id: UUID,
    session: DbSession,
    before: Annotated[str | None, Query(max_length=200)] = None,
    limit: Annotated[int, Query(ge=1, le=MAX_PAGE)] = 50,
    form_key: FormKey = None,
    number: Annotated[int | None, Query(ge=1, le=MAX_NUMBER)] = None,
) -> LeadList:
    """От новых к старым; `before` — `next_before` предыдущей страницы; `number` — заявка
    с этим номером."""
    try:
        page = await list_leads(
            TenantId(tenant_id),
            LeadRepository(session),
            before=before,
            limit=limit,
            where=LeadFilter(form_key=form_key, number=number),
        )
    except InvalidCursorError as e:
        raise ApiError(status.HTTP_422_UNPROCESSABLE_CONTENT, "invalid_input", str(e)) from e
    return LeadList(
        leads=[_summary(lead) for lead in page.items],
        next_before=encode_cursor(page.next_cursor) if page.next_cursor else None,
    )


@router.get("/leads.csv", dependencies=[AdminViewer], response_class=Response)
async def leads_export(
    tenant_id: UUID,
    session: DbSession,
    created_from: Annotated[datetime | None, Query(alias="from")] = None,
    created_to: Annotated[datetime | None, Query(alias="to")] = None,
    form_key: FormKey = None,
) -> Response:
    """CSV для Excel (ADR-0045 п.4): `from` — включительно, `to` — не включая; время без
    пояса — 422. Больше MAX_EXPORT заявок — 422."""
    for moment in (created_from, created_to):
        if moment is not None and moment.tzinfo is None:
            raise ApiError(
                status.HTTP_422_UNPROCESSABLE_CONTENT,
                "invalid_input",
                "время периода — с часовым поясом",
            )
    where = LeadFilter(form_key=form_key, created_from=created_from, created_to=created_to)
    try:
        exported = await export_leads(TenantId(tenant_id), LeadRepository(session), where)
    except (InvalidPeriodError, TooManyLeadsError) as e:
        raise ApiError(status.HTTP_422_UNPROCESSABLE_CONTENT, "invalid_input", str(e)) from e
    # Кто и что выгрузил — без значений полей (admin_user_id и tenant_id уже в контексте).
    logger.info(
        "admin.leads_exported",
        rows=len(exported),
        form_key=form_key,
        created_from=created_from.isoformat() if created_from else None,
        created_to=created_to.isoformat() if created_to else None,
    )
    return Response(
        content=leads_csv(exported),
        media_type="text/csv; charset=utf-8",
        headers={
            "Content-Disposition": 'attachment; filename="leads.csv"',
            "Cache-Control": "no-store",
        },
    )


def _summary(lead: Lead) -> LeadSummary:
    return LeadSummary(
        id=lead.id,
        number=lead.number,
        created_at=lead.created_at,
        form_key=lead.form_key,
        form_title=lead.form_title,
        conversation_id=lead.conversation_id,
        fields=[
            LeadFieldValue(name=name, label=label, value=value)
            for name, label, value in lead.ordered_fields()
        ],
    )
