"""Каталог тенанта для инструментов `search_catalog`/`get_entity`/`show_entities` (порт tools
`Catalog`).

Каждый вызов — своя короткая сессия из фабрики: инструменты хода не делят `AsyncSession`
стрима, где пишутся сообщения, и могут выполняться параллельно. Только чтение, commit нет.
"""

from collections.abc import Callable, Sequence
from uuid import UUID

from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.knowledge.domain.catalog import CatalogEntity, CatalogPage, CatalogQuery
from app.modules.knowledge.domain.errors import CatalogError
from app.modules.knowledge.infrastructure.repositories import EntityRepository
from app.modules.shared.public import TenantId


class SqlCatalog:
    """Сбой БД — `CatalogError` (инструмент превратит его в `upstream_error`)."""

    def __init__(self, session_factory: Callable[[], AsyncSession]) -> None:
        self._session_factory = session_factory

    async def search(self, tenant_id: TenantId, query: CatalogQuery) -> CatalogPage:
        try:
            async with self._session_factory() as session:
                return await EntityRepository(session).search(tenant_id, query)
        except SQLAlchemyError as error:
            raise CatalogError(f"поиск по каталогу: {error}") from error

    async def get(self, tenant_id: TenantId, entity_id: UUID) -> CatalogEntity | None:
        try:
            async with self._session_factory() as session:
                return await EntityRepository(session).get_catalog_entity(tenant_id, entity_id)
        except SQLAlchemyError as error:
            raise CatalogError(f"сущность каталога: {error}") from error

    async def get_many(
        self, tenant_id: TenantId, entity_ids: Sequence[UUID]
    ) -> list[CatalogEntity]:
        try:
            async with self._session_factory() as session:
                return await EntityRepository(session).list_catalog_entities(tenant_id, entity_ids)
        except SQLAlchemyError as error:
            raise CatalogError(f"сущности каталога: {error}") from error
