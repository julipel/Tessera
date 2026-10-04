"""OpenAIEmbedder (P4-06a) без сети: настоящий AsyncOpenAI поверх MockTransport."""

import json
from typing import Any

import httpx2
import pytest
from openai import AsyncOpenAI

from app.modules.knowledge.public import Embedder, EmbeddingError, OpenAIEmbedder


def vector(text: str, dimensions: int = 3) -> list[float]:
    return [float(len(text))] * dimensions


class Server:
    """Отвечает эмбеддингами входных текстов (в обратном порядке `data`) и запоминает запросы."""

    def __init__(self, *, status: int = 200, dimensions: int = 3, drop_last: bool = False) -> None:
        self.status = status
        self.dimensions = dimensions
        self.drop_last = drop_last
        self.requests: list[dict[str, Any]] = []

    def __call__(self, request: httpx2.Request) -> httpx2.Response:
        body = json.loads(request.content)
        self.requests.append(body)
        if self.status != 200:
            return httpx2.Response(self.status, json={"error": {"message": "boom"}})
        texts: list[str] = body["input"]
        if self.drop_last:
            texts = texts[:-1]
        data = [
            {"object": "embedding", "index": i, "embedding": vector(t, self.dimensions)}
            for i, t in enumerate(texts)
        ]
        return httpx2.Response(
            200,
            json={
                "object": "list",
                "data": data[::-1],
                "model": body["model"],
                "usage": {"prompt_tokens": 1, "total_tokens": 1},
            },
        )


def embedder_for(server: Server, *, batch_size: int = 2) -> OpenAIEmbedder:
    client = AsyncOpenAI(
        api_key="sk-test",
        base_url="http://openai.test/v1",
        max_retries=0,
        http_client=httpx2.AsyncClient(transport=httpx2.MockTransport(server)),
    )
    return OpenAIEmbedder(client, model="emb-test", dimensions=3, batch_size=batch_size)


async def test_embeds_in_batches_preserving_order() -> None:
    server = Server()
    embedder: Embedder = embedder_for(server, batch_size=2)
    texts = ["a", "bb", "ccc", "dddd", "eeeee"]

    vectors = await embedder.embed(texts)

    assert vectors == [vector(t) for t in texts]
    assert [r["input"] for r in server.requests] == [["a", "bb"], ["ccc", "dddd"], ["eeeee"]]
    assert all(r["model"] == "emb-test" for r in server.requests)
    assert all("dimensions" not in r for r in server.requests)


async def test_empty_input_makes_no_requests() -> None:
    server = Server()

    assert await embedder_for(server).embed([]) == []
    assert server.requests == []


async def test_wrong_dimensions_raise() -> None:
    with pytest.raises(EmbeddingError, match="размерность 4 вместо 3"):
        await embedder_for(Server(dimensions=4)).embed(["a"])


async def test_missing_vectors_raise() -> None:
    with pytest.raises(EmbeddingError, match="ожидалось 2 векторов, получено 1"):
        await embedder_for(Server(drop_last=True)).embed(["a", "b"])


async def test_api_error_becomes_embedding_error() -> None:
    with pytest.raises(EmbeddingError, match="emb-test"):
        await embedder_for(Server(status=500)).embed(["a"])


def test_batch_size_must_be_positive() -> None:
    with pytest.raises(ValueError):
        embedder_for(Server(), batch_size=0)
