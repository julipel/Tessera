"""Публичный интерфейс модуля tenants — единственная точка входа для других модулей."""

from app.modules.tenants.application.seed import SeedResult, seed_tenant
from app.modules.tenants.application.spec import TenantSpec, load_tenant_spec
from app.modules.tenants.domain.entities import (
    AgentConfigStatus,
    AgentConfigVersion,
    Tenant,
    TenantStatus,
)
from app.modules.tenants.domain.errors import AgentConfigNotFoundError, InvalidTenantSpecError
from app.modules.tenants.domain.widget_keys import generate_widget_key, hash_widget_key
from app.modules.tenants.infrastructure.models import (
    AgentConfigRecord,
    TenantRecord,
    WidgetKeyRecord,
)
from app.modules.tenants.infrastructure.repositories import (
    AgentConfigRepository,
    SqlTenantDirectory,
    WidgetKeyRepository,
)

__all__ = [
    "AgentConfigNotFoundError",
    "AgentConfigRecord",
    "AgentConfigRepository",
    "AgentConfigStatus",
    "AgentConfigVersion",
    "InvalidTenantSpecError",
    "SeedResult",
    "SqlTenantDirectory",
    "Tenant",
    "TenantRecord",
    "TenantSpec",
    "TenantStatus",
    "WidgetKeyRecord",
    "WidgetKeyRepository",
    "generate_widget_key",
    "hash_widget_key",
    "load_tenant_spec",
    "seed_tenant",
]
