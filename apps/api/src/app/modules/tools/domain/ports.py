"""Порты зависимостей встроенных инструментов (реализации — в других модулях, подключает
тот, кто собирает агент)."""

from collections.abc import Mapping, Sequence
from typing import Protocol
from uuid import UUID

from app.modules.knowledge.kernel import CatalogEntity, CatalogPage, CatalogQuery, ChunkHit
from app.modules.shared.kernel import TenantId


class KnowledgeSearcher(Protocol):
    """Поиск по знаниям тенанта (knowledge `KnowledgeSearch`). Ошибки — `EmbeddingError`,
    `VectorIndexError` из knowledge.kernel."""

    async def search(
        self, tenant_id: TenantId, query: str, *, top_k: int, rerank: bool
    ) -> list[ChunkHit]: ...


class Catalog(Protocol):
    """Каталог Entity тенанта (knowledge `SqlCatalog`). Ошибка — `CatalogError` из
    knowledge.kernel; чужая или несуществующая сущность — None."""

    async def search(self, tenant_id: TenantId, query: CatalogQuery) -> CatalogPage: ...

    async def get(self, tenant_id: TenantId, entity_id: UUID) -> CatalogEntity | None: ...

    async def get_many(
        self, tenant_id: TenantId, entity_ids: Sequence[UUID]
    ) -> list[CatalogEntity]:
        """Найденные сущности тенанта в любом порядке; чужих и несуществующих нет."""
        ...


class LeadStore(Protocol):
    """Заявки тенанта (leads `SqlLeadStore`). Возвращает id созданной заявки."""

    async def create(
        self,
        tenant_id: TenantId,
        conversation_id: UUID,
        form_key: str,
        fields: Mapping[str, str],
    ) -> UUID: ...
