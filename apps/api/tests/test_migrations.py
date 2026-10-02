"""Миграции применяются и откатываются."""

import asyncio

from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine


async def _db_revision(url: str) -> str | None:
    engine = create_async_engine(url)
    try:
        async with engine.connect() as conn:
            has_table = await conn.scalar(text("SELECT to_regclass('alembic_version')"))
            if has_table is None:
                return None
            version: str | None = await conn.scalar(text("SELECT version_num FROM alembic_version"))
            return version
    finally:
        await engine.dispose()


async def test_upgrade_downgrade_upgrade(db_url: str, alembic_cfg: Config) -> None:
    head = ScriptDirectory.from_config(alembic_cfg).get_current_head()

    # env.py сам вызывает asyncio.run — команды Alembic запускаем вне текущего loop.
    await asyncio.to_thread(command.downgrade, alembic_cfg, "base")
    assert await _db_revision(db_url) is None

    await asyncio.to_thread(command.upgrade, alembic_cfg, "head")
    assert await _db_revision(db_url) == head
