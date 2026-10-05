"""Заявка пользователя (create_lead, ADR-0021): значения полей формы тенанта."""

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from app.modules.shared.kernel import TenantId


@dataclass(frozen=True, slots=True)
class Lead:
    """`form_key` — форма из `forms` конфига, по полям которой собрана заявка."""

    id: UUID
    tenant_id: TenantId
    conversation_id: UUID
    form_key: str
    fields: Mapping[str, str]
    created_at: datetime
