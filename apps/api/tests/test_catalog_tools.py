"""Инструменты search_catalog и get_entity (P4-08b): схема аргументов по filterable_attributes,
перевод аргументов в CatalogQuery, content для модели, ошибки, подключение через
builtin_tools, агентный цикл на FakeLLM и сквозной вызов на Postgres."""

import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any
from uuid import UUID

import pytest
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncSession, async_sessionmaker

from app.contracts import AgentConfig, CatalogConfig
from app.modules.agent.public import (
    FakeLLM,
    FakeReply,
    RegistryToolExecutor,
    ToolCall,
    ToolResultMessage,
)
from app.modules.chat.domain.entities import TurnRequest
from app.modules.chat.infrastructure.loop_agent import LoopTurnAgent
from app.modules.knowledge.public import (
    AttributeFilter,
    CatalogEntity,
    CatalogError,
    CatalogFilters,
    CatalogPage,
    CatalogQuery,
    CatalogSort,
    EntityRecord,
    EntityRepository,
    SourceKind,
    SourceRecord,
    SourceRepository,
    SqlCatalog,
)
from app.modules.shared.kernel import TenantId
from app.modules.tenants.public import SqlTenantDirectory
from app.modules.tools.public import (
    GET_ENTITY,
    SEARCH_CATALOG,
    SHOW_ENTITIES,
    UPDATE_DIALOG_STATE,
    ToolContext,
    ToolDefinition,
    ToolInvocation,
    ToolRegistry,
    ToolResult,
    builtin_tools,
    get_entity_tool,
    search_catalog_tool,
    show_entities_tool,
)

TENANT = TenantId(uuid.uuid4())

CREAM = CatalogEntity(
    id=uuid.UUID("00000000-0000-0000-0000-000000000001"),
    type="product",
    title="Крем для сухой кожи",
    price=Decimal("1990.00"),
    currency="RUB",
    in_stock=True,
    category="Уход за лицом",
    url="https://shop.example/cream",
    image_url="https://shop.example/cream.jpg",
    attributes={"skin_type": "сухая", "volume_ml": 50},
)
SERUM = CatalogEntity(
    id=uuid.UUID("00000000-0000-0000-0000-000000000002"),
    type="product",
    title="Сыворотка",
    price=Decimal("2590.50"),
)


@dataclass
class FakeCatalog:
    entities: list[CatalogEntity] = field(default_factory=lambda: [CREAM, SERUM])
    total: int | None = None
    fail: bool = False
    searches: list[tuple[TenantId, CatalogQuery]] = field(default_factory=list)
    gets: list[tuple[TenantId, UUID]] = field(default_factory=list)
    batches: list[tuple[TenantId, list[UUID]]] = field(default_factory=list)

    async def search(self, tenant_id: TenantId, query: CatalogQuery) -> CatalogPage:
        self.searches.append((tenant_id, query))
        if self.fail:
            raise CatalogError("connection refused")
        items = self.entities[: query.limit]
        return CatalogPage(
            items=items, total=len(self.entities) if self.total is None else self.total
        )

    async def get(self, tenant_id: TenantId, entity_id: UUID) -> CatalogEntity | None:
        self.gets.append((tenant_id, entity_id))
        if self.fail:
            raise CatalogError("connection refused")
        return next((e for e in self.entities if e.id == entity_id), None)

    async def get_many(
        self, tenant_id: TenantId, entity_ids: Sequence[UUID]
    ) -> list[CatalogEntity]:
        self.batches.append((tenant_id, list(entity_ids)))
        if self.fail:
            raise CatalogError("connection refused")
        # Порядок каталога, а не запроса: инструмент сам расставляет позиции.
        return [e for e in self.entities if e.id in entity_ids]


def ctx(tenant_id: TenantId = TENANT) -> ToolContext:
    return ToolContext(tenant_id=tenant_id, conversation_id=uuid.uuid4(), turn_id=uuid.uuid4())


async def call(
    tool: ToolDefinition, arguments: dict[str, Any], tenant_id: TenantId = TENANT
) -> ToolResult:
    [result] = await ToolRegistry([tool]).execute_many(
        [ToolInvocation(id="call_1", name=tool.name, arguments=arguments)], ctx(tenant_id)
    )
    return result


def search_tool(catalog: FakeCatalog, attributes: list[str] | None = None) -> ToolDefinition:
    settings = CatalogConfig(filterable_attributes=attributes) if attributes is not None else None
    return search_catalog_tool(catalog, settings)


# --- search_catalog: схема и аргументы ---


def test_attributes_schema_lists_only_filterable_attributes() -> None:
    with_attributes = search_tool(FakeCatalog(), ["skin_type", "volume_ml", "skin_type"])
    without = search_tool(FakeCatalog())

    filters = with_attributes.parameters["properties"]["filters"]["properties"]
    assert list(filters["attributes"]["properties"]) == ["skin_type", "volume_ml"]
    assert "attributes" not in without.parameters["properties"]["filters"]["properties"]


async def test_type_enum_from_entity_types() -> None:
    types = {"skincare": "уход за лицом", "fragrance": "парфюмерия"}
    with_types = search_catalog_tool(FakeCatalog(), CatalogConfig(entity_types=types))
    without = search_tool(FakeCatalog())

    type_schema = with_types.parameters["properties"]["filters"]["properties"]["type"]
    assert type_schema["enum"] == ["skincare", "fragrance"]
    assert "skincare — уход за лицом" in type_schema["description"]
    assert "enum" not in without.parameters["properties"]["filters"]["properties"]["type"]
    result = await call(with_types, {"filters": {"type": "product"}})
    assert result.error is not None and result.error.code == "validation_error"


async def test_arguments_become_catalog_query() -> None:
    catalog = FakeCatalog()
    arguments = {
        "query": "крем",
        "filters": {
            "type": "product",
            "category": ["Уход за лицом", "Волосы"],
            "price_min": 1000,
            "price_max": 2999.9,
            "in_stock": True,
            "attributes": {
                "skin_type": "сухая",
                "brand": ["Glow", "Nord"],
                "volume_ml": {"min": 30, "max": 100.5},
                "vegan": True,
            },
        },
        "sort": "price_asc",
        "limit": 5,
    }

    result = await call(
        search_tool(catalog, ["skin_type", "brand", "volume_ml", "vegan"]), arguments
    )

    assert result.ok
    [(tenant_id, query)] = catalog.searches
    assert tenant_id == TENANT
    assert query == CatalogQuery(
        query="крем",
        filters=CatalogFilters(
            types=("product",),
            categories=("Уход за лицом", "Волосы"),
            price_min=Decimal("1000"),
            price_max=Decimal("2999.9"),
            in_stock=True,
            attributes={
                "skin_type": AttributeFilter(any_of=("сухая",)),
                "brand": AttributeFilter(any_of=("Glow", "Nord")),
                "volume_ml": AttributeFilter(min=Decimal("30"), max=Decimal("100.5")),
                "vegan": AttributeFilter(any_of=(True,)),
            },
        ),
        sort=CatalogSort.PRICE_ASC,
        limit=5,
    )


async def test_defaults_without_arguments() -> None:
    catalog = FakeCatalog()

    await call(search_tool(catalog), {})

    [(_, query)] = catalog.searches
    assert query == CatalogQuery(limit=10, sort=CatalogSort.RELEVANCE)


@pytest.mark.parametrize(
    "arguments",
    [
        {"filters": {"attributes": {"color": "синий"}}},  # не в filterable_attributes
        {"filters": {"attributes": {"skin_type": {}}}},  # пустой диапазон
        {"filters": {"attributes": {"skin_type": []}}},
        {"filters": {"attributes": {"volume_ml": {"min": "30"}}}},
        {"filters": {"price_min": -1}},
        {"filters": {"category": "Уход"}},  # список, а не строка
        {"filters": {"brand": "Glow"}},  # атрибут не на своём месте
        {"sort": "popular"},
        {"limit": 0},
        {"limit": 21},
        {"query": ""},
        {"text": "крем"},
    ],
)
async def test_invalid_arguments_are_validation_errors(arguments: dict[str, Any]) -> None:
    catalog = FakeCatalog()

    result = await call(search_tool(catalog, ["skin_type", "volume_ml"]), arguments)

    assert result.error is not None and result.error.code == "validation_error"
    assert catalog.searches == []


# --- search_catalog: результат ---


async def test_content_is_compact() -> None:
    catalog = FakeCatalog(total=37)

    result = await call(search_tool(catalog), {"query": "крем"})

    assert result.content == {
        "total": 37,
        "items": [
            {
                "id": "00000000-0000-0000-0000-000000000001",
                "title": "Крем для сухой кожи",
                "price": 1990,
                "currency": "RUB",
                "in_stock": True,
                "category": "Уход за лицом",
                "url": "https://shop.example/cream",
                "attributes": {"skin_type": "сухая", "volume_ml": 50},
            },
            # Пустые поля опущены; дробная цена — float.
            {"id": "00000000-0000-0000-0000-000000000002", "title": "Сыворотка", "price": 2590.5},
        ],
    }
    assert result.components == ()


async def test_nothing_found_is_not_an_error() -> None:
    result = await call(search_tool(FakeCatalog(entities=[])), {"query": "телескоп"})

    assert result.ok
    assert isinstance(result.content, dict)
    assert result.content["total"] == 0 and result.content["items"] == []
    assert "ослабь фильтры" in result.content["note"]


async def test_search_failure_is_retryable_upstream_error() -> None:
    result = await call(search_tool(FakeCatalog(fail=True)), {"query": "крем"})

    assert result.error is not None
    assert result.error.code == "upstream_error" and result.error.retryable
    assert "connection refused" not in result.error.message


# --- get_entity ---


async def test_get_entity_returns_all_fields() -> None:
    catalog = FakeCatalog()

    result = await call(get_entity_tool(catalog), {"entity_id": str(CREAM.id)})

    assert result.content == {
        "id": str(CREAM.id),
        "title": "Крем для сухой кожи",
        "type": "product",
        "price": 1990,
        "currency": "RUB",
        "in_stock": True,
        "category": "Уход за лицом",
        "url": "https://shop.example/cream",
        "image_url": "https://shop.example/cream.jpg",
        "attributes": {"skin_type": "сухая", "volume_ml": 50},
    }
    assert catalog.gets == [(TENANT, CREAM.id)]


@pytest.mark.parametrize("entity_id", [str(uuid.uuid4()), "sku-1", "Крем для сухой кожи"])
async def test_unknown_entity_is_not_found(entity_id: str) -> None:
    result = await call(get_entity_tool(FakeCatalog()), {"entity_id": entity_id})

    assert result.error is not None
    assert result.error.code == "not_found" and not result.error.retryable
    assert "search_catalog" in result.error.message


async def test_get_entity_failure_is_upstream_error() -> None:
    result = await call(get_entity_tool(FakeCatalog(fail=True)), {"entity_id": str(CREAM.id)})

    assert result.error is not None and result.error.code == "upstream_error"


# --- подключение и агентный цикл ---


def agent_config(builtin: list[str], knowledge: dict[str, Any] | None = None) -> dict[str, Any]:
    config: dict[str, Any] = {
        "assistant": {"name": "A", "greeting": "Привет!", "fallback_message": "Не вышло."},
        "model": {"primary": {"provider": "openai", "name": "m"}},
        "limits": {},
        "prompt": {"tenant": "Ты — консультант."},
        "tools": {"builtin": builtin},
    }
    if knowledge is not None:
        config["knowledge"] = knowledge
    return config


def test_builtin_tools_need_catalog() -> None:
    config = AgentConfig.model_validate(
        agent_config(
            [SEARCH_CATALOG, GET_ENTITY, SHOW_ENTITIES, UPDATE_DIALOG_STATE],
            {"catalog": {"filterable_attributes": ["skin_type"]}},
        )
    )

    with_catalog = builtin_tools(config, catalog=FakeCatalog())
    without = builtin_tools(config)

    assert [t.name for t in with_catalog] == [
        SEARCH_CATALOG,
        GET_ENTITY,
        SHOW_ENTITIES,
        UPDATE_DIALOG_STATE,
    ]
    filters = with_catalog[0].parameters["properties"]["filters"]["properties"]
    assert list(filters["attributes"]["properties"]) == ["skin_type"]
    assert [t.name for t in without] == [UPDATE_DIALOG_STATE]


async def test_agent_loop_passes_catalog_items_to_model() -> None:
    catalog = FakeCatalog()
    lookup = ToolCall(
        id="call_1",
        name=SEARCH_CATALOG,
        arguments={"query": "крем", "filters": {"in_stock": True}},
        raw_arguments='{"query": "крем", "filters": {"in_stock": true}}',
    )
    llm = FakeLLM([FakeReply(tool_calls=(lookup,)), FakeReply(text="Есть крем за 1990 ₽.")])
    agent = LoopTurnAgent(
        lambda _: llm,
        lambda c, _: RegistryToolExecutor(ToolRegistry(builtin_tools(c, catalog=catalog))),
    )
    request = TurnRequest(
        tenant_id=TENANT,
        conversation_id=uuid.uuid4(),
        agent_config_id=uuid.uuid4(),
        turn_id=uuid.uuid4(),
        input={"type": "text", "text": "Есть крем для сухой кожи?"},
        agent_config=agent_config([SEARCH_CATALOG, GET_ENTITY]),
        history=(),
    )

    _ = [event async for event in agent.run_turn(request)]

    first, second = llm.requests
    assert [t.name for t in first.tools] == [SEARCH_CATALOG, GET_ENTITY]
    [tool_message] = [m for m in second.messages if isinstance(m, ToolResultMessage)]
    assert "Крем для сухой кожи" in tool_message.content
    [(tenant_id, query)] = catalog.searches
    assert tenant_id == TENANT and query.filters.in_stock is True


def test_app_wires_sql_catalog(app: FastAPI) -> None:
    assert isinstance(app.state.catalog, SqlCatalog)


# --- сквозной вызов: инструмент → SqlCatalog → Postgres ---


async def test_tools_over_sql_catalog(
    db_connection: AsyncConnection, db_session: AsyncSession
) -> None:
    tenant_id = (await SqlTenantDirectory(db_session).create("catalog-tools", "t")).id
    other_id = (await SqlTenantDirectory(db_session).create("catalog-tools-b", "t")).id
    rows: dict[str, EntityRecord] = {}
    for tid, external_id, title, attributes in [
        (tenant_id, "cream", "Крем для сухой кожи", {"skin_type": "Сухая"}),
        (tenant_id, "gel", "Гель для жирной кожи", {"skin_type": "Жирная"}),
        (other_id, "foreign", "Крем тенанта B", {"skin_type": "Сухая"}),
    ]:
        source = await SourceRepository(db_session).add(
            tid, SourceRecord(tenant_id=tid, kind=SourceKind.TABLE, config={})
        )
        rows[external_id] = await EntityRepository(db_session).add(
            tid,
            EntityRecord(
                tenant_id=tid,
                source_id=source.id,
                external_id=external_id,
                type="product",
                title=title,
                price=Decimal("990"),
                in_stock=True,
                attributes=attributes,
                content_hash="0" * 64,
            ),
        )
    await db_session.flush()
    catalog = SqlCatalog(
        async_sessionmaker(bind=db_connection, join_transaction_mode="create_savepoint")
    )
    search = search_catalog_tool(catalog, CatalogConfig(filterable_attributes=["skin_type"]))

    found = await call(
        search, {"query": "крем", "filters": {"attributes": {"skin_type": "сухая"}}}, tenant_id
    )
    entity = await call(get_entity_tool(catalog), {"entity_id": str(rows["gel"].id)}, tenant_id)
    foreign = await call(
        get_entity_tool(catalog), {"entity_id": str(rows["foreign"].id)}, tenant_id
    )

    assert found.content == {
        "total": 1,
        "items": [
            {
                "id": str(rows["cream"].id),
                "title": "Крем для сухой кожи",
                "price": 990,
                "in_stock": True,
                "attributes": {"skin_type": "Сухая"},
            }
        ],
    }
    assert isinstance(entity.content, dict) and entity.content["title"] == "Гель для жирной кожи"
    assert foreign.error is not None and foreign.error.code == "not_found"

    shown = await call(
        show_entities_tool(catalog),
        {
            "entity_ids": [str(rows["gel"].id), str(rows["foreign"].id), str(rows["cream"].id)],
            "layout": "carousel",
        },
        tenant_id,
    )
    assert isinstance(shown.content, dict)
    assert [item["title"] for item in shown.content["shown"]] == [
        "Гель для жирной кожи",
        "Крем для сухой кожи",
    ]
    assert shown.content["not_found"] == [str(rows["foreign"].id)]
