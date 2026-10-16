"""Базовый репозиторий: каждый запрос ограничен тенантом."""

from collections.abc import Sequence
from typing import Any, cast
from uuid import UUID

from sqlalchemy import ColumnElement, CursorResult, Select, delete, func, select
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

    async def _delete_batch(
        self,
        tenant_id: TenantId,
        *criteria: ColumnElement[bool],
        limit: int,
        skip_locked: bool = False,
    ) -> int:
        """Удаляет не больше `limit` строк тенанта по условиям — порция для фоновой чистки
        (ADR-0043): короткая транзакция, без блокировки всей выборки. `skip_locked` —
        строки, занятые другими транзакциями, пропускаются. Возвращает число строк."""
        ids = select(self.model.id).where(self.model.tenant_id == tenant_id, *criteria).limit(limit)
        if skip_locked:
            ids = ids.with_for_update(skip_locked=True)
        stmt = (
            delete(self.model)
            .where(self.model.tenant_id == tenant_id, self.model.id.in_(ids.scalar_subquery()))
            # Объекты в сессии не держим — синхронизировать нечего, без лишнего RETURNING.
            .execution_options(synchronize_session=False)
        )
        result = cast(CursorResult[Any], await self.session.execute(stmt))
        return result.rowcount

    async def _count(self, tenant_id: TenantId, *criteria: ColumnElement[bool]) -> int:
        stmt = (
            select(func.count())
            .select_from(self.model)
            .where(self.model.tenant_id == tenant_id, *criteria)
        )
        return int((await self.session.execute(stmt)).scalar_one())

    async def delete(self, tenant_id: TenantId, id: UUID) -> bool:
        obj = await self.get(tenant_id, id)
        if obj is None:
            return False
        await self.session.delete(obj)
        await self.session.flush()
        return True
