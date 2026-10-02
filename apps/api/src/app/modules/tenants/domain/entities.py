"""Сущности тенанта и версии конфигурации агента."""

from dataclasses import dataclass
from enum import StrEnum
from typing import Any
from uuid import UUID

from app.modules.shared.kernel import TenantId


class TenantStatus(StrEnum):
    ACTIVE = "active"
    DISABLED = "disabled"


class AgentConfigStatus(StrEnum):
    """draft → active → archived. Активной может быть только одна версия на тенанта."""

    DRAFT = "draft"
    ACTIVE = "active"
    ARCHIVED = "archived"


@dataclass(frozen=True, slots=True)
class Tenant:
    id: TenantId
    slug: str
    name: str
    status: TenantStatus


@dataclass(frozen=True, slots=True)
class AgentConfigVersion:
    """Версия конфигурации. `config` — JSON, валидный по контракту AgentConfig."""

    id: UUID
    tenant_id: TenantId
    version: int
    status: AgentConfigStatus
    config: dict[str, Any]


@dataclass(frozen=True, slots=True)
class WidgetAccess:
    """Результат проверки ключа виджета: тенант и сайты, с которых ключ разрешён."""

    tenant_id: TenantId
    allowed_origins: tuple[str, ...]
