"""Данные тенантов для эвалов (ADR-0011 п.5): `tenant_id` из БД по slug и каталог.

Конфиг по-прежнему из YAML, диалоги в БД не пишутся. Нет БД или тенанта в ней — раннер берёт
синтетический id, и инструменты знаний и каталога работают с пустым тенантом.
"""

import asyncio
from collections.abc import Iterable
from dataclasses import dataclass, field

from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncEngine

from app.modules.knowledge.public import SqlCatalog
from app.modules.shared.public import TenantId, create_engine, create_session_factory
from app.modules.tenants.public import SqlTenantDirectory
from app.settings import Settings

# В WSL подключение к закрытому порту 127.0.0.1 может висеть — ждать дольше бессмысленно.
CONNECT_TIMEOUT_S = 5.0


@dataclass(slots=True)
class TenantData:
    ids: dict[str, TenantId] = field(default_factory=dict)
    catalog: SqlCatalog | None = None
    error: str | None = None  # почему БД не использована
    _engine: AsyncEngine | None = None

    async def aclose(self) -> None:
        if self._engine is not None:
            await self._engine.dispose()


async def resolve_tenant_ids(
    directory: SqlTenantDirectory, slugs: Iterable[str]
) -> dict[str, TenantId]:
    """slug → id для тенантов, которые есть в БД."""
    ids: dict[str, TenantId] = {}
    for slug in slugs:
        tenant = await directory.get_by_slug(slug)
        if tenant is not None:
            ids[slug] = tenant.id
    return ids


async def connect_tenant_data(
    settings: Settings, slugs: Iterable[str], timeout_s: float = CONNECT_TIMEOUT_S
) -> TenantData:
    """Каталог подключается, только если найден хотя бы один тенант."""
    engine = create_engine(settings.database_url)
    session_factory = create_session_factory(engine)
    try:
        async with asyncio.timeout(timeout_s), session_factory() as session:
            ids = await resolve_tenant_ids(SqlTenantDirectory(session), slugs)
    except (TimeoutError, OSError, SQLAlchemyError) as e:
        await engine.dispose()
        return TenantData(error=f"БД недоступна ({type(e).__name__})")
    if not ids:
        await engine.dispose()
        return TenantData(error="тенантов нет в БД — make seed")
    return TenantData(ids, SqlCatalog(session_factory), _engine=engine)
