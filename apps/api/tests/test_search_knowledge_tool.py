"""Инструмент search_knowledge (P4-07b): фрагменты для модели, компонент sources, ошибки,
подключение через builtin_tools и проход через агентный цикл на FakeLLM."""

import uuid
from typing import Any

import pytest

from app.contracts import AgentConfig, Component, KnowledgeSearchConfig
from app.knowledge_wiring import build_knowledge_services
from app.modules.agent.public import (
    ComponentEmitted,
    FakeLLM,
    FakeReply,
    RegistryToolExecutor,
    ToolCall,
    ToolResultMessage,
)
from app.modules.chat.domain.entities import TurnRequest
from app.modules.chat.infrastructure.loop_agent import LoopTurnAgent
from app.modules.knowledge.public import IndexedChunk, IndexedDocument, KnowledgeSearch
from app.modules.shared.kernel import TenantId
from app.modules.tools.public import (
    SEARCH_KNOWLEDGE,
    UPDATE_DIALOG_STATE,
    ToolContext,
    ToolInvocation,
    ToolRegistry,
    ToolResult,
    builtin_tools,
    search_knowledge_tool,
)
from app.settings import Settings
from knowledge_fakes import FakeEmbedder, FakeReranker, InMemoryChunkIndex

TENANT = TenantId(uuid.uuid4())
OTHER = TenantId(uuid.uuid4())
SOURCE = uuid.uuid4()


def add(
    index: InMemoryChunkIndex,
    external_id: str,
    *texts: str,
    tenant_id: TenantId = TENANT,
    url: str | None = "auto",
    section: str | None = None,
) -> None:
    index.documents[(tenant_id, SOURCE, external_id)] = IndexedDocument(
        document_id=uuid.uuid4(),
        external_id=external_id,
        title=f"Документ {external_id}",
        url=f"https://shop.example/{external_id}" if url == "auto" else url,
        chunks=[
            IndexedChunk(chunk_id=uuid.uuid4(), ord=n, text=text, dense=[1.0, 1.0], section=section)
            for n, text in enumerate(texts)
        ],
    )


def ctx(tenant_id: TenantId = TENANT) -> ToolContext:
    return ToolContext(tenant_id=tenant_id, conversation_id=uuid.uuid4(), turn_id=uuid.uuid4())


async def call(
    index: InMemoryChunkIndex,
    arguments: dict[str, Any],
    *,
    settings: KnowledgeSearchConfig | None = None,
    embedder: FakeEmbedder | None = None,
    reranker: FakeReranker | None = None,
    tenant_id: TenantId = TENANT,
) -> ToolResult:
    search = KnowledgeSearch(embedder or FakeEmbedder(), index, reranker)
    registry = ToolRegistry([search_knowledge_tool(search, settings)])
    [result] = await registry.execute_many(
        [ToolInvocation(id="call_1", name=SEARCH_KNOWLEDGE, arguments=arguments)], ctx(tenant_id)
    )
    return result


async def test_fragments_for_model_and_sources_component() -> None:
    index = InMemoryChunkIndex()
    add(index, "delivery", "доставка курьером 1-2 дня", section="Доставка > Сроки")
    add(index, "faq", "доставка без url", url=None)

    result = await call(index, {"query": "доставка"})

    assert result.ok
    assert result.content == {
        "fragments": [
            {
                "n": 1,
                "title": "Документ delivery",
                "section": "Доставка > Сроки",
                "url": "https://shop.example/delivery",
                "text": "доставка курьером 1-2 дня",
            },
            {"n": 2, "title": "Документ faq", "text": "доставка без url"},
        ]
    }
    [component] = result.components
    assert component == {
        "type": "sources",
        "items": [
            {
                "title": "Документ delivery",
                "url": "https://shop.example/delivery",
                "snippet": "доставка курьером 1-2 дня",
            }
        ],
    }
    Component.model_validate(component)


async def test_sources_dedup_by_url_and_snippet_is_shortened() -> None:
    index = InMemoryChunkIndex()
    long_text = "оплата " + "картой\n\n  или наличными " * 30
    add(index, "payment", long_text, "оплата частями")

    result = await call(index, {"query": "оплата"})

    assert len(result.content["fragments"]) == 2  # type: ignore[index]
    [component] = result.components
    [item] = component["items"]
    assert len(item["snippet"]) == 200
    assert item["snippet"].endswith("…")
    assert "\n" not in item["snippet"] and "  " not in item["snippet"]


async def test_no_sources_component_without_urls() -> None:
    index = InMemoryChunkIndex()
    add(index, "notes", "возврат 14 дней", url=None)

    result = await call(index, {"query": "возврат"})

    assert result.ok and result.components == ()


async def test_nothing_found_is_not_an_error() -> None:
    result = await call(InMemoryChunkIndex(), {"query": "гарантия"})

    assert result.ok
    assert result.content == {"fragments": [], "note": "в базе знаний ничего не найдено"}
    assert result.components == ()


async def test_top_k_default_from_config_and_from_arguments() -> None:
    index = InMemoryChunkIndex()
    add(index, "bonus", *[f"бонусы правило {n}" for n in range(8)])
    settings = KnowledgeSearchConfig(top_k=3, rerank=False)

    by_default = await call(index, {"query": "бонусы"}, settings=settings)
    explicit = await call(index, {"query": "бонусы", "top_k": 5}, settings=settings)

    assert len(by_default.content["fragments"]) == 3  # type: ignore[index]
    assert len(explicit.content["fragments"]) == 5  # type: ignore[index]


async def test_rerank_flag_comes_from_config() -> None:
    index = InMemoryChunkIndex()
    add(index, "a", "контакты магазина", "контакты склада")
    enabled, disabled = FakeReranker(), FakeReranker()

    await call(index, {"query": "контакты"}, reranker=enabled)
    await call(
        index,
        {"query": "контакты"},
        settings=KnowledgeSearchConfig(rerank=False),
        reranker=disabled,
    )

    assert len(enabled.calls) == 1
    assert disabled.calls == []


@pytest.mark.parametrize(
    "arguments",
    [{}, {"query": ""}, {"query": "x", "top_k": 0}, {"query": "x", "top_k": 11}, {"q": "x"}],
)
async def test_invalid_arguments(arguments: dict[str, Any]) -> None:
    result = await call(InMemoryChunkIndex(), arguments)

    assert result.error is not None and result.error.code == "validation_error"


@pytest.mark.parametrize("failing", ["embedder", "index"])
async def test_backend_failure_is_retryable_upstream_error(failing: str) -> None:
    index = InMemoryChunkIndex(fail_search=failing == "index")
    embedder = FakeEmbedder(fail=failing == "embedder")

    result = await call(index, {"query": "доставка"}, embedder=embedder)

    assert result.error is not None
    assert (result.error.code, result.error.retryable) == ("upstream_error", True)
    assert "Qdrant" not in result.error.message and "провайдер" not in result.error.message


async def test_searches_tenant_from_context() -> None:
    index = InMemoryChunkIndex()
    add(index, "secret", "промокод тенанта B", tenant_id=OTHER)

    result = await call(index, {"query": "промокод"})

    assert result.content == {"fragments": [], "note": "в базе знаний ничего не найдено"}
    assert index.searches[0][0] == TENANT


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


def test_builtin_tools_need_knowledge_search() -> None:
    config = AgentConfig.model_validate(
        agent_config([SEARCH_KNOWLEDGE, UPDATE_DIALOG_STATE], {"search_knowledge": {"top_k": 4}})
    )
    search = KnowledgeSearch(FakeEmbedder(), InMemoryChunkIndex())

    with_search = builtin_tools(config, knowledge=search)
    without = builtin_tools(config)

    assert [t.name for t in with_search] == [SEARCH_KNOWLEDGE, UPDATE_DIALOG_STATE]
    assert with_search[0].parameters["properties"]["top_k"]["description"].endswith("4).")
    assert [t.name for t in without] == [UPDATE_DIALOG_STATE]
    disabled = AgentConfig.model_validate(agent_config([UPDATE_DIALOG_STATE]))
    assert [t.name for t in builtin_tools(disabled, knowledge=search)] == [UPDATE_DIALOG_STATE]


async def test_agent_loop_emits_sources_and_passes_fragments_to_model() -> None:
    index = InMemoryChunkIndex()
    add(index, "delivery", "доставка курьером 300 рублей")
    search = KnowledgeSearch(FakeEmbedder(), index)
    lookup = ToolCall(
        id="call_1",
        name=SEARCH_KNOWLEDGE,
        arguments={"query": "доставка"},
        raw_arguments='{"query": "доставка"}',
    )
    llm = FakeLLM([FakeReply(tool_calls=(lookup,)), FakeReply(text="Доставка — 300 рублей.")])
    agent = LoopTurnAgent(
        lambda _: llm,
        lambda c, _: RegistryToolExecutor(ToolRegistry(builtin_tools(c, knowledge=search))),
    )
    request = TurnRequest(
        tenant_id=TENANT,
        conversation_id=uuid.uuid4(),
        agent_config_id=uuid.uuid4(),
        turn_id=uuid.uuid4(),
        input={"type": "text", "text": "Сколько стоит доставка?"},
        agent_config=agent_config([SEARCH_KNOWLEDGE]),
        history=(),
    )

    events = [event async for event in agent.run_turn(request)]

    [component] = [e.component for e in events if isinstance(e, ComponentEmitted)]
    assert component["type"] == "sources"
    assert component["items"][0]["url"] == "https://shop.example/delivery"
    first, second = llm.requests
    assert [t.name for t in first.tools] == [SEARCH_KNOWLEDGE]
    [tool_message] = [m for m in second.messages if isinstance(m, ToolResultMessage)]
    assert "доставка курьером 300 рублей" in tool_message.content


def test_knowledge_services_need_openai_key() -> None:
    assert build_knowledge_services(Settings(_env_file=None, openai_api_key=None)) is None


async def test_knowledge_services_are_built_without_network() -> None:
    settings = Settings(_env_file=None, openai_api_key="sk-test", qdrant_url="http://qdrant:6333")

    services = build_knowledge_services(settings)

    assert services is not None
    assert services.index.collection == settings.qdrant_collection
    assert services.embedder.dimensions == settings.embedding_dimensions
    await services.aclose()
