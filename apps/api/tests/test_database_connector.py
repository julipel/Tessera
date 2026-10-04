"""Коннектор `database`: маппинг строк, read-only, лимиты, DSN и проверка адреса (ADR-0018).

«База клиента» — временная схема в тестовой БД; подключение к localhost разрешено
allowlist подсетей, как при `SOURCE_DB_ALLOWED_NETWORKS=["127.0.0.0/8"]`.
"""

from collections.abc import AsyncIterator
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

import asyncpg
import pytest
from sqlalchemy import make_url
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.knowledge.public import (
    DatabaseConnector,
    DatabaseSourceError,
    DocumentItem,
    EntityItem,
    EntityRepository,
    MarkdownChunker,
    RawItemRef,
    SourceConnector,
    SourceKind,
    SourceRecord,
    SourceRepository,
    SourceSecrets,
    SourceSpec,
    SourceSyncRecord,
    SourceSyncRepository,
    SqlSyncStore,
    SyncStatus,
    parse_networks,
    run_sync,
)
from app.modules.shared.public import TenantId
from app.modules.tenants.public import SqlTenantDirectory
from knowledge_fakes import FakeEmbedder, InMemoryChunkIndex

LOCAL = parse_networks(["127.0.0.0/8", "::1/128"])
UID = UUID("00000000-0000-0000-0000-000000000001")


@pytest.fixture
async def client_db(db_url: str) -> AsyncIterator[tuple[str, str]]:
    """(DSN, схема) «базы клиента» с таблицей products."""
    dsn = make_url(db_url).set(drivername="postgresql").render_as_string(hide_password=False)
    schema = f"client_{uuid4().hex[:8]}"
    connection = await asyncpg.connect(dsn)
    try:
        await connection.execute(
            f"""
            CREATE SCHEMA {schema};
            CREATE TABLE {schema}.products (
                sku text, name text, price numeric, qty int, brand text, tags text[], uid uuid
            );
            INSERT INTO {schema}.products VALUES
                ('A-1', 'Крем', 1990.50, 3, 'Acme', '{{new}}', '{UID}'),
                ('A-2', 'Тоник', NULL, 0, NULL, NULL, NULL);
            """
        )
        yield dsn, schema
    finally:
        await connection.execute(f"DROP SCHEMA {schema} CASCADE")
        await connection.close()


def connector(dsn: str, **kwargs: Any) -> DatabaseConnector:
    kwargs.setdefault("allowed_networks", LOCAL)
    return DatabaseConnector(SourceSecrets({"SOURCE_SECRET_DB": dsn}), **kwargs)


def spec(schema: str, query: str | None = None, **config: Any) -> SourceSpec:
    base: dict[str, Any] = {
        "dsn_env": "SOURCE_SECRET_DB",
        "sslmode": "disable",
        "query": query or f"SELECT * FROM {schema}.products ORDER BY sku",
        "entity": {
            "entity_type": "product",
            "currency": "RUB",
            "fields": {"external_id": "sku", "title": "name", "price": "price", "in_stock": "qty"},
            "attributes": {"brand": "brand", "uid": "uid"},
        },
    }
    return SourceSpec(TenantId(uuid4()), uuid4(), {**base, **config})


async def items(db: DatabaseConnector, source: SourceSpec) -> dict[str, Any]:
    listing = await db.discover(source)
    assert listing.cursor is None
    result: dict[str, Any] = {}
    for ref in listing.refs:
        try:
            result[ref.external_id] = await db.fetch(source, ref)
        except DatabaseSourceError as e:
            result[ref.external_id] = str(e)
    return result


async def test_maps_rows_to_entities(client_db: tuple[str, str]) -> None:
    dsn, schema = client_db

    result = await items(connector(dsn), spec(schema))

    assert result == {
        "A-1": EntityItem(
            external_id="A-1",
            type="product",
            title="Крем",
            price=Decimal("1990.50"),
            currency="RUB",
            in_stock=True,
            attributes={"brand": "Acme", "uid": str(UID)},
        ),
        "A-2": EntityItem(
            external_id="A-2", type="product", title="Тоник", currency="RUB", in_stock=False
        ),
    }


async def test_maps_rows_to_documents_and_row_errors(client_db: tuple[str, str]) -> None:
    dsn, schema = client_db
    query = f"""
        SELECT sku, name, name || ': ' || coalesce(brand, '') AS body, tags FROM {schema}.products
        UNION ALL SELECT NULL, 'Без id', 'x', NULL
        UNION ALL SELECT 'A-2', 'Дубль', 'x', NULL
    """
    document = {"external_id": "sku", "title": "name", "text": "body"}

    result = await items(connector(dsn), spec(schema, query, entity=None, document=document))

    assert result["A-1"] == DocumentItem(external_id="A-1", title="Крем", text="Крем: Acme")
    assert result["#3"] == "запись 3: пустой id («sku»)"
    assert result["A-2"] == "id «A-2» повторяется: записи 2, 4"

    entity = {"entity_type": "p", "fields": {"external_id": "sku", "title": "tags"}}
    errors = await items(connector(dsn), spec(schema, query, entity=entity))
    assert "«tags»: не скалярное значение (list)" in errors["A-1"]


@pytest.mark.parametrize(
    "query",
    [
        "WITH d AS (DELETE FROM {s}.products RETURNING *) SELECT * FROM d",
        "SELECT * FROM {s}.products; DELETE FROM {s}.products",
        "DELETE FROM {s}.products RETURNING *",
    ],
)
async def test_writes_are_rejected(client_db: tuple[str, str], query: str) -> None:
    dsn, schema = client_db

    with pytest.raises(DatabaseSourceError):
        await connector(dsn).discover(spec(schema, query.format(s=schema)))

    connection = await asyncpg.connect(dsn)
    try:
        assert await connection.fetchval(f"SELECT count(*) FROM {schema}.products") == 2
    finally:
        await connection.close()


async def test_statement_timeout(client_db: tuple[str, str]) -> None:
    dsn, schema = client_db
    query = f"SELECT p.* FROM {schema}.products p, pg_sleep(2)"

    with pytest.raises(DatabaseSourceError, match=r"QueryCanceled|нет ответа"):
        await connector(dsn).discover(spec(schema, query, timeout_s=0.3))


async def test_more_than_max_rows_is_source_error(client_db: tuple[str, str]) -> None:
    dsn, schema = client_db

    with pytest.raises(DatabaseSourceError, match="max_rows=1"):
        await connector(dsn).discover(spec(schema, max_rows=1))


async def test_missing_mapped_column_is_source_error(client_db: tuple[str, str]) -> None:
    dsn, schema = client_db
    query = f"SELECT sku, name FROM {schema}.products WHERE false"

    with pytest.raises(DatabaseSourceError, match="нет колонок «price», «qty», «brand», «uid»"):
        await connector(dsn).discover(spec(schema, query))


async def test_private_address_without_allowlist_is_rejected(client_db: tuple[str, str]) -> None:
    dsn, schema = client_db

    with pytest.raises(DatabaseSourceError, match="SOURCE_DB_ALLOWED_NETWORKS"):
        await connector(dsn, allowed_networks=[]).discover(spec(schema))


@pytest.mark.parametrize(
    "addresses",
    [["8.8.8.8", "10.1.0.5"], ["10.1.0.5"], ["::ffff:127.0.0.1"], ["169.254.169.254"], []],
)
async def test_any_disallowed_address_rejects_host(addresses: list[str]) -> None:
    async def resolver(host: str, port: int) -> list[str]:
        return addresses

    db = connector(
        "postgresql://u:p@db.client.ru/shop",
        allowed_networks=parse_networks(["10.0.0.0/24"]),
        resolver=resolver,
    )

    with pytest.raises(DatabaseSourceError, match="SOURCE_DB_ALLOWED_NETWORKS"):
        await db.discover(spec("s"))


@pytest.mark.parametrize(
    "dsn",
    [
        "postgresql://u:secret@h/db?sslmode=disable",
        "postgresql://u:secret@h1,h2/db",
        "postgresql://u@h/db",
        "postgresql://u:secret@h/",
        "mysql://u:secret@h/db",
        "postgresql://u:secret@h:99999/db",
    ],
)
async def test_bad_dsn_is_source_error_without_secret_in_message(dsn: str) -> None:
    with pytest.raises(DatabaseSourceError, match="DSN") as error:
        await connector(dsn).discover(spec("s"))
    assert "secret" not in str(error.value)


@pytest.mark.parametrize(
    "config",
    [
        {"dsn_env": "DATABASE_URL"},
        {"entity": None},
        {"sslmode": "verify-full"},
        {"timeout_s": 3600},
    ],
)
async def test_bad_config_or_secret_is_source_error(config: dict[str, Any]) -> None:
    with pytest.raises(DatabaseSourceError, match=r"конфиг|разрешены только"):
        await connector("postgresql://u:p@h/db").discover(spec("s", **config))


async def test_fetch_reloads_without_cache(client_db: tuple[str, str]) -> None:
    dsn, schema = client_db
    source = spec(schema)

    item = await connector(dsn).fetch(source, RawItemRef("A-1"))

    assert item.title == "Крем"
    with pytest.raises(DatabaseSourceError, match="не найдена"):
        await connector(dsn).fetch(source, RawItemRef("Z"))


def test_satisfies_protocol() -> None:
    proto: SourceConnector = connector("postgresql://u:p@h/db")
    assert proto.kind is SourceKind.DATABASE


async def test_sync_stores_entities(client_db: tuple[str, str], db_session: AsyncSession) -> None:
    dsn, schema = client_db
    tenant_id = (await SqlTenantDirectory(db_session).create("db", "db")).id
    config = dict(spec(schema).config)
    source = await SourceRepository(db_session).add(
        tenant_id, SourceRecord(tenant_id=tenant_id, kind=SourceKind.DATABASE, config=config)
    )
    record = await SourceSyncRepository(db_session).add(
        tenant_id, SourceSyncRecord(tenant_id=tenant_id, source_id=source.id)
    )

    await run_sync(
        tenant_id,
        record.id,
        SqlSyncStore(db_session),
        {SourceKind.DATABASE: connector(dsn)},
        MarkdownChunker(),
        embedder=FakeEmbedder(),
        index=InMemoryChunkIndex(),
        full=True,
    )

    record = await SourceSyncRepository(db_session).get_or_raise(tenant_id, record.id)
    await db_session.refresh(record)
    assert record.status is SyncStatus.SUCCEEDED, record.error
    assert record.stats["created"] == 2
    entities = {e.external_id: e for e in await EntityRepository(db_session).list(tenant_id)}
    assert entities["A-1"].price == Decimal("1990.50")
