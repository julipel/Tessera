"""Сущности тенанта и версии конфигурации агента."""

from dataclasses import dataclass
from datetime import datetime
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


class AgentConfigOrigin(StrEnum):
    """Откуда версия: YAML из `config/tenants` (seed) или редактор админки. Seed не
    активирует YAML поверх правок админки (REVIEW.md: R-1, дополнение ADR-0043)."""

    SEED = "seed"
    ADMIN = "admin"


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
    created_at: datetime
    origin: AgentConfigOrigin


@dataclass(frozen=True, slots=True)
class Activation:
    """Итог активации: активная версия и номер прежней активной (None — активной не было;
    равен `config.version` — версия уже была активной)."""

    config: AgentConfigVersion
    previous_version: int | None


@dataclass(frozen=True, slots=True)
class WidgetAccess:
    """Результат проверки ключа виджета: тенант и сайты, с которых ключ разрешён."""

    tenant_id: TenantId
    allowed_origins: tuple[str, ...]
