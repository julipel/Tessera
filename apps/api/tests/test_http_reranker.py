"""HttpReranker (P4-07c, ADR-0015): формат Cohere/Jina `/rerank` на httpx.MockTransport,
сборка реранкера из настроек."""

import json
from collections.abc import Callable
from typing import Any

import httpx
import pytest

from app.knowledge_wiring import build_knowledge_services, build_reranker
from app.modules.knowledge.public import (
    HttpReranker,
    Reranker,
    RerankError,
    create_http_reranker,
)
from app.settings import Settings

URL = "https://rerank.test/v2/rerank"

type Handler = Callable[[httpx.Request], httpx.Response]


def reranker(handler: Handler, *, api_key: str | None = None) -> HttpReranker:
    """Заголовки — от фабрики, транспорт — мок (без прокси из окружения)."""
    factory_headers = create_http_reranker(URL, model="rerank-v3.5", api_key=api_key).http.headers
    http = httpx.AsyncClient(headers=factory_headers, transport=httpx.MockTransport(handler))
    return HttpReranker(http, url=URL, model="rerank-v3.5")


def answer(results: Any, status: int = 200) -> Handler:
    def handle(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, json={"id": "r1", "results": results})

    return handle


async def test_request_body_and_auth_header() -> None:
    seen: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"results": [{"index": 1, "relevance_score": 0.9}]})

    port: Reranker = reranker(handle, api_key="secret")

    assert await port.rerank("доставка", ["оплата", "доставка курьером"], 1) == [1]
    [request] = seen
    assert (request.method, str(request.url)) == ("POST", URL)
    assert request.headers["Authorization"] == "Bearer secret"
    assert json.loads(request.content) == {
        "model": "rerank-v3.5",
        "query": "доставка",
        "documents": ["оплата", "доставка курьером"],
        "top_n": 1,
    }


async def test_no_auth_header_without_key() -> None:
    seen: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"results": []})

    await reranker(handle).rerank("q", ["a"], 1)

    assert "Authorization" not in seen[0].headers


async def test_orders_by_relevance_score_and_cuts_top_n() -> None:
    """Jina отдаёт и `document`, и score вне [0, 1] — лишние поля игнорируются."""
    results = [
        {"index": 0, "relevance_score": 0.1},
        {"index": 2, "relevance_score": 22.3, "document": {"text": "c"}},
        {"index": 1, "relevance_score": 5.0},
    ]

    ranked = await reranker(answer(results)).rerank("q", ["a", "b", "c"], 2)

    assert ranked == [2, 1]


async def test_empty_texts_skip_request() -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        raise AssertionError("запроса быть не должно")

    assert await reranker(handle).rerank("q", [], 3) == []


@pytest.mark.parametrize(
    "handler",
    [
        answer([], status=401),
        answer([], status=503),
        lambda r: httpx.Response(200, text="not json"),
        lambda r: httpx.Response(200, json={"data": []}),
        answer({"index": 0}),
        answer([{"index": "0", "relevance_score": 1.0}]),
        answer([{"index": True, "relevance_score": 1.0}]),
        answer([{"index": 0}]),
    ],
    ids=[
        "401",
        "503",
        "not-json",
        "no-results",
        "results-not-list",
        "str-index",
        "bool-index",
        "no-score",
    ],
)
async def test_bad_responses_raise_rerank_error(handler: Handler) -> None:
    with pytest.raises(RerankError):
        await reranker(handler).rerank("q", ["a"], 1)


async def test_network_error_raises_rerank_error() -> None:
    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    with pytest.raises(RerankError, match="ConnectError"):
        await reranker(refuse).rerank("q", ["a"], 1)


def settings(**values: Any) -> Settings:
    return Settings(_env_file=None, openai_api_key="sk-test", **values)


async def test_reranker_is_built_with_url_and_model() -> None:
    built = build_reranker(
        settings(rerank_url=URL, rerank_model="jina-reranker-v2", rerank_api_key="k")
    )

    assert built is not None
    assert (built.url, built.model) == (URL, "jina-reranker-v2")
    assert built.http.headers["Authorization"] == "Bearer k"
    await built.aclose()


@pytest.mark.parametrize(
    "values",
    [{}, {"rerank_url": URL}, {"rerank_model": "m"}, {"rerank_url": "", "rerank_model": ""}],
)
def test_reranker_is_disabled_without_url_or_model(values: dict[str, Any]) -> None:
    assert build_reranker(settings(**values)) is None


async def test_knowledge_services_own_reranker() -> None:
    services = build_knowledge_services(settings(rerank_url=URL, rerank_model="m"))

    assert services is not None and services.reranker is not None
    await services.aclose()
    assert services.reranker.http.is_closed
