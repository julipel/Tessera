"""Базовый репозиторий: каждый запрос ограничен тенантом."""

from collections.abc import Sequence
from uuid import UUID

from sqlalchemy import ColumnElement, Select, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.shared.domain.errors import NotFoundError, TenantMismatchError
from app.modules.shared.domain.ids import TenantId
from app.modules.shared.infrastructure.db import TenantScopedBase


class TenantRepository[ModelT: TenantScopedBase]:
    """Наследники задают `model`; все методы требуют tenant_id первым аргументом.

    Репозиторий делает flush, но не commit — транзакцией управляет вызывающий код.
    """

    model: type[ModelT]

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    def _scoped(self, tenant_id: TenantId) -> Select[ModelT]:
        # Единственная точка построения запросов — фильтр по тенанту не забыть.
        return select(self.model).where(self.model.tenant_id == tenant_id)

    async def get(self, tenant_id: TenantId, id: UUID) -> ModelT | None:
        stmt = self._scoped(tenant_id).where(self.model.id == id)
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def get_or_raise(self, tenant_id: TenantId, id: UUID) -> ModelT:
        obj = await self.get(tenant_id, id)
        if obj is None:
            raise NotFoundError(f"{self.model.__name__} {id} not found")
        return obj

    async def list(self, tenant_id: TenantId, *criteria: ColumnElement[bool]) -> Sequence[ModelT]:
        stmt = self._scoped(tenant_id).where(*criteria)
        return (await self.session.execute(stmt)).scalars().all()

    async def add(self, tenant_id: TenantId, obj: ModelT) -> ModelT:
        if obj.tenant_id != tenant_id:
            raise TenantMismatchError(f"{self.model.__name__}: tenant_id не совпадает")
        self.session.add(obj)
        await self.session.flush()
        return obj

    async def delete(self, tenant_id: TenantId, id: UUID) -> bool:
        obj = await self.get(tenant_id, id)
        if obj is None:
            return False
        await self.session.delete(obj)
        await self.session.flush()
        return True
