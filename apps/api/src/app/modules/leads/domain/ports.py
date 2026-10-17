"""Порты модуля leads."""

from typing import Protocol

from app.modules.leads.domain.lead import Lead, LeadCursor, LeadFilter
from app.modules.shared.kernel import TenantId


class LeadReader(Protocol):
    async def list_page(
        self,
        tenant_id: TenantId,
        *,
        before: LeadCursor | None,
        limit: int,
        where: LeadFilter,
    ) -> list[Lead]:
        """Заявки тенанта от новых к старым, строго раньше `before`, не больше `limit`."""
        ...
