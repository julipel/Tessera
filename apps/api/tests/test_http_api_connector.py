"""Коннектор `http_api`: маппинг записей, пагинация, секреты, ошибки источника и элементов."""

import base64
import json
from decimal import Decimal
from typing import Any
from uuid import uuid4

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.knowledge.public import (
    DocumentItem,
    DocumentRepository,
    EntityItem,
    EntityRepository,
    HttpApiConnector,
    HttpApiSourceError,
    MarkdownChunker,
    RawItemRef,
    SourceConnector,
    SourceKind,
    SourceRecord,
    SourceRepository,
    SourceSecretError,
    SourceSecrets,
    SourceSpec,
    SourceSyncRecord,
    SourceSyncRepository,
    SqlSyncStore,
    SyncStatus,
    WebClient,
    run_sync,
)
from app.modules.shared.public import TenantId
from app.modules.tenants.public import SqlTenantDirectory
from knowledge_fakes import FakeEmbedder, InMemoryChunkIndex

API = "https://api.example.ru"
ENTITY = {
    "entity_type": "product",
    "currency": "rub",
    "fields": {
        "external_id": "id",
        "title": "name",
        "price": "price.value",
        "in_stock": "stock",
        "image_url": "images.0.url",
    },
    "attributes": {"brand": "brand.name", "volume_ml": {"path": "volume", "type": "number"}},
}


def product(id_: Any, name: str = "Крем", **extra: Any) -> dict[str, Any]:
    return {"id": id_, "name": name, **extra}


class Api:
    """API на `httpx.MockTransport`: обработчик получает запрос и отдаёт (статус, тело)."""

    def __init__(self, handler: Any, env: dict[str, str] | None = None) -> None:
        self.handler = handler
        self.env = env or {}
        self.requests: list[httpx.Request] = []

    def _respond(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        result = self.handler(request)
        if isinstance(result, httpx.Response):
            return result
        status, body = result
        content = body if isinstance(body, bytes) else json.dumps(body).encode()
        headers = {"Content-Type": "application/json"}
        return httpx.Response(status, headers=headers, stream=httpx.ByteStream(content))

    def connector(self) -> HttpApiConnector:
        http = httpx.AsyncClient(transport=httpx.MockTransport(self._respond))
        return HttpApiConnector(
            WebClient(http, user_agent="TesseraBot/0.1"), SourceSecrets(self.env)
        )


def static(body: Any, status: int = 200) -> Any:
    return lambda request: (status, body)


def spec(**config: Any) -> SourceSpec:
    base: dict[str, Any] = {"url": f"{API}/v1/products", "entity": ENTITY}
    return SourceSpec(TenantId(uuid4()), uuid4(), {**base, **config})


async def items(api: Api, **config: Any) -> dict[str, EntityItem | DocumentItem | str]:
    """external_id → элемент или текст ошибки элемента."""
    connector = api.connector()
    source = spec(**config)
    listing = await connector.discover(source)
    assert listing.cursor is None
    result: dict[str, EntityItem | DocumentItem | str] = {}
    for ref in listing.refs:
        try:
            result[ref.external_id] = await connector.fetch(source, ref)
        except HttpApiSourceError as e:
            result[ref.external_id] = str(e)
    return result


async def test_maps_records_by_paths() -> None:
    body = {
        "data": {
            "items": [
                product(
                    101,
                    price={"value": "1 990,50"},
                    stock=3,
                    images=[{"url": "https://cdn/1.jpg"}],
                    brand={"name": "Acme"},
                    volume="50",
                ),
                product("A-2", "Тоник", stock="нет"),
            ]
        }
    }
    api = Api(static(body))

    result = await items(api, records="data.items", params={"lang": "ru", "active": True})

    assert result["101"] == EntityItem(
        external_id="101",
        type="product",
        title="Крем",
        price=Decimal("1990.50"),
        currency="RUB",
        in_stock=True,
        image_url="https://cdn/1.jpg",
        attributes={"brand": "Acme", "volume_ml": 50},
    )
    assert result["A-2"] == EntityItem(
        external_id="A-2", type="product", title="Тоник", currency="RUB", in_stock=False
    )
    assert api.requests[0].url.params == httpx.QueryParams({"lang": "ru", "active": "true"})


async def test_list_attributes_from_arrays_and_strings() -> None:
    entity = {
        **ENTITY,
        "attributes": {
            "skin_types": {"path": "skin", "type": "list"},
            "notes": {"path": "notes", "type": "list", "separator": "/"},
            "brand": "brand",
        },
    }
    body = [
        product("A", skin=["dry", "oily"], notes="роза / мускус"),
        product("B", skin=[{"type": "dry"}]),
        product("C", brand=["Acme"]),
    ]

    result = await items(Api(static(body)), entity=entity)

    assert isinstance(result["A"], EntityItem)
    assert result["A"].attributes == {"skin_types": ["dry", "oily"], "notes": ["роза", "мускус"]}
    assert "«skin»: не скалярное значение" in str(result["B"])
    assert "список, а нужно одно значение" in str(result["C"])


async def test_maps_documents() -> None:
    body = [
        {"slug": "faq-1", "q": "Доставка", "a": "Курьером за 2 дня.", "link": "https://x.ru/1"},
        {"slug": "faq-2", "q": "Оплата", "a": ""},
    ]
    document = {"external_id": "slug", "title": "q", "text": "a", "url": "link"}

    result = await items(Api(static(body)), entity=None, document=document)

    assert result["faq-1"] == DocumentItem(
        external_id="faq-1", title="Доставка", text="Курьером за 2 дня.", url="https://x.ru/1"
    )
    assert "пустой текст" in str(result["faq-2"])


async def test_record_errors_are_item_errors() -> None:
    body = [
        product("A"),
        product(None),
        "строка",
        product("B", name=" "),
        product("C", price={"value": "-5"}),
        product("D", brand={"name": {"nested": 1}}),
        product("E"),
        product("E", "Другой"),
    ]

    result = await items(Api(static(body)))

    assert isinstance(result["A"], EntityItem)
    assert result["#2"] == "запись 2: пустой id («id»)"
    assert result["#3"] == "запись 3: не объект"
    assert "пустое название" in str(result["B"])
    assert "цена: отрицательная" in str(result["C"])
    assert "«brand.name»: не скалярное значение" in str(result["D"])
    assert result["E"] == "id «E» повторяется: записи 7, 8"


async def test_page_pagination_stops_on_short_page() -> None:
    def handler(request: httpx.Request) -> Any:
        page = int(request.url.params["p"])
        size = int(request.url.params["size"])
        assert size == 2
        records = {1: [product("1"), product("2")], 2: [product("3")]}[page]
        return 200, records

    pagination = {"type": "page", "param": "p", "size_param": "size", "page_size": 2}
    api = Api(handler)

    assert sorted(await items(api, pagination=pagination)) == ["1", "2", "3"]
    assert len(api.requests) == 2


async def test_page_pagination_without_size_stops_on_empty_page() -> None:
    pages = {0: [product("1")], 1: [product("2")], 2: []}
    api = Api(lambda r: (200, pages[int(r.url.params["page"])]))

    result = await items(api, pagination={"type": "page", "start": 0})

    assert sorted(result) == ["1", "2"]


async def test_offset_pagination() -> None:
    data = [product(str(i)) for i in range(5)]

    def handler(request: httpx.Request) -> Any:
        offset, limit = int(request.url.params["offset"]), int(request.url.params["limit"])
        return 200, {"items": data[offset : offset + limit]}

    pagination = {"type": "offset", "size_param": "limit", "page_size": 2}
    api = Api(handler)

    assert len(await items(api, records="items", pagination=pagination)) == 5
    assert [r.url.params["offset"] for r in api.requests] == ["0", "2", "4"]


async def test_cursor_pagination() -> None:
    pages = {None: ([product("1")], "c2"), "c2": ([product("2")], "c3"), "c3": ([], None)}

    def handler(request: httpx.Request) -> Any:
        records, cursor = pages[request.url.params.get("after")]
        return 200, {"items": records, "meta": {"next": cursor}}

    pagination = {"type": "cursor", "param": "after", "cursor_path": "meta.next"}
    result = await items(Api(handler), records="items", pagination=pagination)

    assert sorted(result) == ["1", "2"]


async def test_repeated_cursor_is_source_error() -> None:
    api = Api(static({"items": [product("1")], "next": "same"}))
    pagination = {"type": "cursor", "cursor_path": "next"}

    with pytest.raises(HttpApiSourceError, match="пагинация зациклена"):
        await api.connector().discover(spec(records="items", pagination=pagination))


async def test_next_url_pagination_relative_links() -> None:
    def handler(request: httpx.Request) -> Any:
        if request.url.params.get("page") == "2":
            return 200, {"items": [product("2")], "links": {"next": None}}
        return 200, {"items": [product("1")], "links": {"next": "/v1/products?page=2"}}

    pagination = {"type": "next_url", "next_url_path": "links.next"}
    result = await items(Api(handler), records="items", pagination=pagination)

    assert sorted(result) == ["1", "2"]


async def test_next_url_to_other_host_is_source_error() -> None:
    api = Api(static({"items": [], "next": "https://evil.ru/steal"}))
    pagination = {"type": "next_url", "next_url_path": "next"}

    with pytest.raises(HttpApiSourceError, match="за пределы API"):
        await api.connector().discover(spec(records="items", pagination=pagination))


async def test_more_than_max_pages_is_source_error() -> None:
    api = Api(lambda r: (200, [product(r.url.params["page"])]))
    pagination = {"type": "page", "max_pages": 3}

    with pytest.raises(HttpApiSourceError, match="max_pages=3"):
        await api.connector().discover(spec(pagination=pagination))
    assert len(api.requests) == 3


async def test_more_than_max_records_is_source_error() -> None:
    api = Api(static([product(str(i)) for i in range(4)]))

    with pytest.raises(HttpApiSourceError, match="max_records=3"):
        await api.connector().discover(spec(max_records=3))


@pytest.mark.parametrize(
    ("auth", "env", "header", "value"),
    [
        (
            {"type": "bearer", "secret_env": "SOURCE_SECRET_ACME"},
            {"SOURCE_SECRET_ACME": "tok"},
            "Authorization",
            "Bearer tok",
        ),
        (
            {"type": "header", "header": "X-Api-Key", "secret_env": "SOURCE_SECRET_ACME"},
            {"SOURCE_SECRET_ACME": "key"},
            "X-Api-Key",
            "key",
        ),
        (
            {"type": "basic", "secret_env": "SOURCE_SECRET_ACME"},
            {"SOURCE_SECRET_ACME": "user:pass"},
            "Authorization",
            "Basic " + base64.b64encode(b"user:pass").decode(),
        ),
    ],
)
async def test_auth_from_source_secret(
    auth: dict[str, str], env: dict[str, str], header: str, value: str
) -> None:
    api = Api(static([]), env=env)

    await api.connector().discover(spec(auth=auth, headers={"Accept": "application/json"}))

    assert api.requests[0].headers[header] == value
    assert api.requests[0].headers["Accept"] == "application/json"


@pytest.mark.parametrize(
    ("secret_env", "message"),
    [("OPENAI_API_KEY", "разрешены только"), ("SOURCE_SECRET_MISSING", "не задана")],
)
async def test_bad_secret_is_source_error_without_request(secret_env: str, message: str) -> None:
    api = Api(static([]), env={"OPENAI_API_KEY": "sk-platform"})

    with pytest.raises(HttpApiSourceError, match=message):
        await api.connector().discover(spec(auth={"type": "bearer", "secret_env": secret_env}))
    assert api.requests == []


async def test_auth_not_sent_on_redirect_to_other_host() -> None:
    def handler(request: httpx.Request) -> Any:
        if request.url.host == "api.example.ru":
            return httpx.Response(302, headers={"Location": "https://evil.ru/x"})
        return 200, []

    api = Api(handler, env={"SOURCE_SECRET_ACME": "tok"})
    auth = {"type": "bearer", "secret_env": "SOURCE_SECRET_ACME"}

    with pytest.raises(HttpApiSourceError, match="редирект за пределы сайта"):
        await api.connector().discover(spec(auth=auth))
    assert [r.url.host for r in api.requests] == ["api.example.ru"]


@pytest.mark.parametrize(
    ("handler", "message"),
    [
        (static({"error": "x"}, status=500), "HTTP 500"),
        (static(b"<html>not json</html>"), "ответ не JSON"),
        (static({"data": {}}), "нет массива записей по пути «data.items»"),
    ],
)
async def test_bad_response_is_source_error(handler: Any, message: str) -> None:
    with pytest.raises(HttpApiSourceError, match=message):
        await Api(handler).connector().discover(spec(records="data.items"))


@pytest.mark.parametrize(
    "config",
    [
        {"entity": None},
        {"document": {"external_id": "id", "title": "t", "text": "x"}},
        {"headers": {"Authorization": "Bearer inline"}},
        {"auth": {"type": "header", "secret_env": "SOURCE_SECRET_X"}},
        {"pagination": {"type": "offset"}},
        {"pagination": {"type": "cursor"}},
        {"url": "ftp://api.example.ru/"},
    ],
)
async def test_bad_config_is_source_error(config: dict[str, Any]) -> None:
    api = Api(static([]))

    with pytest.raises(HttpApiSourceError, match=r"конфиг|url"):
        await api.connector().discover(spec(**config))
    assert api.requests == []


async def test_fetch_uses_discover_cache_and_reloads_without_it() -> None:
    api = Api(static([product("A")]))
    connector = api.connector()
    source = spec()

    await connector.discover(source)
    await connector.fetch(source, RawItemRef("A"))
    assert len(api.requests) == 1

    other = api.connector()  # новый процесс воркера: кэша нет
    assert (await other.fetch(source, RawItemRef("A"))).title == "Крем"
    with pytest.raises(HttpApiSourceError, match="не найдена"):
        await other.fetch(source, RawItemRef("Z"))


def test_source_secrets_only_prefixed_names() -> None:
    secrets = SourceSecrets(
        {"SOURCE_SECRET_A": "1", "DATABASE_URL": "pg://", "SOURCE_SECRET_E": ""}
    )

    assert secrets.get("SOURCE_SECRET_A") == "1"
    for name in ("DATABASE_URL", "source_secret_a", "SOURCE_SECRET_", "SOURCE_SECRET_E"):
        with pytest.raises(SourceSecretError):
            secrets.get(name)


def test_satisfies_protocol() -> None:
    proto: SourceConnector = Api(static([])).connector()
    assert proto.kind is SourceKind.HTTP_API


async def test_sync_stores_entities_and_documents(db_session: AsyncSession) -> None:
    tenant_id = (await SqlTenantDirectory(db_session).create("api", "api")).id
    data = [product("A-1", price={"value": 1990}, brand={"name": "Acme"}), product("A-2", "Тоник")]
    api = Api(lambda r: (200, data))
    connector = api.connector()
    configs = {
        "entity": {"url": f"{API}/v1/products", "entity": ENTITY},
        "document": {
            "url": f"{API}/v1/products",
            "document": {"external_id": "id", "title": "name", "text": "name"},
        },
    }

    async def sync(config: dict[str, Any]) -> dict[str, Any]:
        source = await SourceRepository(db_session).add(
            tenant_id, SourceRecord(tenant_id=tenant_id, kind=SourceKind.HTTP_API, config=config)
        )
        record = await SourceSyncRepository(db_session).add(
            tenant_id, SourceSyncRecord(tenant_id=tenant_id, source_id=source.id)
        )
        sync_id = record.id
        await run_sync(
            tenant_id,
            sync_id,
            SqlSyncStore(db_session),
            {SourceKind.HTTP_API: connector},
            MarkdownChunker(),
            embedder=FakeEmbedder(),
            index=InMemoryChunkIndex(),
            full=True,
        )
        record = await SourceSyncRepository(db_session).get_or_raise(tenant_id, sync_id)
        await db_session.refresh(record)
        assert record.status is SyncStatus.SUCCEEDED, record.error
        return record.stats

    assert (await sync(configs["entity"]))["created"] == 2
    entities = {e.external_id: e for e in await EntityRepository(db_session).list(tenant_id)}
    assert entities["A-1"].price == Decimal("1990.00")
    assert entities["A-1"].attributes == {"brand": "Acme"}

    assert (await sync(configs["document"]))["created"] == 2
    documents = await DocumentRepository(db_session).list(tenant_id)
    assert sorted(d.title for d in documents) == ["Крем", "Тоник"]
