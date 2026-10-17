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


async def test_lead_numbers_backfilled_per_tenant_in_creation_order(
    db_url: str, alembic_cfg: Config
) -> None:
    """0020 (ADR-0045): номера старых заявок — по (created_at, id) внутри тенанта, счётчик
    продолжает с последнего."""
    await asyncio.to_thread(command.downgrade, alembic_cfg, "0019")
    engine = create_async_engine(db_url)
    try:
        async with engine.begin() as conn:
            for slug in ("mig-a", "mig-b"):
                await conn.execute(
                    text(
                        "INSERT INTO tenants (id, slug, name, status) "
                        "VALUES (gen_random_uuid(), :s, :s, 'active')"
                    ),
                    {"s": slug},
                )
            await conn.execute(
                text(
                    "INSERT INTO leads (id, tenant_id, form_key, fields, created_at) "
                    "SELECT gen_random_uuid(), t.id, 'f', jsonb_build_object('n', v.n), v.at "
                    "FROM tenants t, (VALUES ('2', now()), ('1', now() - interval '1 day')) "
                    "AS v(n, at) WHERE t.slug IN ('mig-a', 'mig-b')"
                )
            )
        await asyncio.to_thread(command.upgrade, alembic_cfg, "head")
        async with engine.connect() as conn:
            rows = (
                await conn.execute(
                    text(
                        "SELECT t.slug, l.fields->>'n', l.number, c.last_number FROM leads l "
                        "JOIN tenants t ON t.id = l.tenant_id "
                        "JOIN lead_counters c ON c.tenant_id = l.tenant_id "
                        "WHERE t.slug IN ('mig-a', 'mig-b') ORDER BY t.slug, l.number"
                    )
                )
            ).all()
    finally:
        # И при падении миграции база остаётся на head и без тестовых тенантов — не ломает
        # остальные тесты.
        await asyncio.to_thread(command.upgrade, alembic_cfg, "head")
        async with engine.begin() as conn:
            await conn.execute(text("DELETE FROM tenants WHERE slug IN ('mig-a', 'mig-b')"))
        await engine.dispose()

    assert [tuple(r) for r in rows] == [
        ("mig-a", "1", 1, 2),
        ("mig-a", "2", 2, 2),
        ("mig-b", "1", 1, 2),
        ("mig-b", "2", 2, 2),
    ]
