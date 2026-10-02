"""Порты хранилищ модуля tenants (реализации — в infrastructure)."""

from collections.abc import Sequence
from typing import Any, Protocol
from uuid import UUID

from app.modules.shared.kernel import TenantId
from app.modules.tenants.domain.entities import AgentConfigVersion, Tenant, WidgetAccess


class TenantDirectory(Protocol):
    """Справочник тенантов. Не ограничен тенантом: таблица тенантов — сам список тенантов."""

    async def get_by_slug(self, slug: str) -> Tenant | None: ...

    async def create(self, slug: str, name: str) -> Tenant: ...

    async def rename(self, tenant_id: TenantId, name: str) -> None: ...


class AgentConfigStore(Protocol):
    async def get_active(self, tenant_id: TenantId) -> AgentConfigVersion | None: ...

    async def list_versions(self, tenant_id: TenantId) -> Sequence[AgentConfigVersion]: ...

    async def create_draft(
        self, tenant_id: TenantId, config: dict[str, Any]
    ) -> AgentConfigVersion: ...

    async def activate(self, tenant_id: TenantId, config_id: UUID) -> AgentConfigVersion: ...


class WidgetKeyStore(Protocol):
    async def has_any(self, tenant_id: TenantId) -> bool: ...

    async def delete_all(self, tenant_id: TenantId) -> int: ...

    async def add_key(
        self, tenant_id: TenantId, key_hash: str, allowed_origins: Sequence[str]
    ) -> None: ...


class WidgetKeyResolver(Protocol):
    """Поиск ключа по хэшу среди всех тенантов: до проверки ключа тенант неизвестен (ADR-0007)."""

    async def resolve(self, key_hash: str) -> WidgetAccess | None: ...
