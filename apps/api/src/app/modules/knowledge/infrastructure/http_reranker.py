"""Reranker поверх HTTP `/rerank` в формате Cohere/Jina (ADR-0015): httpx, без SDK.

Общее подмножество Cohere `/v2/rerank`, Jina `/v1/rerank` и совместимых self-hosted серверов
(Infinity, vLLM): запрос `{model, query, documents, top_n}`, ответ
`{results: [{index, relevance_score}]}`. `url` — полный адрес endpoint. Порядок результатов
восстанавливается по `relevance_score` — на сортировку сервера не полагаемся.
"""

from collections.abc import Sequence
from typing import Any

import httpx

from app.modules.knowledge.domain.errors import RerankError


def create_http_reranker(
    url: str, *, model: str, api_key: str | None = None, timeout_s: float = 3.0
) -> "HttpReranker":
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    http = httpx.AsyncClient(headers=headers, timeout=timeout_s)
    return HttpReranker(http, url=url, model=model)


class HttpReranker:
    def __init__(self, http: httpx.AsyncClient, *, url: str, model: str) -> None:
        self.http = http
        self.url = url
        self.model = model

    async def rerank(self, query: str, texts: Sequence[str], top_n: int) -> list[int]:
        if not texts or top_n < 1:
            return []
        body = {"model": self.model, "query": query, "documents": list(texts), "top_n": top_n}
        try:
            response = await self.http.post(self.url, json=body)
        except httpx.HTTPError as error:
            raise RerankError(f"{self.model}: {type(error).__name__}: {error}") from error
        if response.is_error:
            raise RerankError(f"{self.model}: {response.status_code} {response.text[:300]}")
        try:
            ranked = _ranked(response.json())
        except (ValueError, KeyError, TypeError) as error:
            raise RerankError(f"{self.model}: неожиданный ответ: {error!r}") from error
        return ranked[:top_n]

    async def aclose(self) -> None:
        await self.http.aclose()


def _ranked(payload: Any) -> list[int]:
    results = payload["results"]
    if not isinstance(results, list):
        raise TypeError(f"results — {type(results).__name__}, а не список")
    scored: list[tuple[float, int]] = []
    for item in results:
        index = item["index"]
        if not isinstance(index, int) or isinstance(index, bool):
            raise TypeError(f"index — {index!r}")
        scored.append((float(item["relevance_score"]), index))
    scored.sort(key=lambda pair: -pair[0])  # стабильно: при равных score — порядок сервера
    return [index for _, index in scored]
