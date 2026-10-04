"""ChunkIndex поверх REST API Qdrant (httpx, без SDK).

Одна коллекция на окружение, изоляция тенантов — по payload `tenant_id` (ADR-0006):
каждый фильтр начинается с `tenant_id`, индекс `tenant_id` помечен `is_tenant`, а HNSW
строится по тенантам (`payload_m`) без общего графа (`m: 0`) — поиск без фильтра по
тенанту не поддерживается.

Векторы точки: `dense` (cosine, размерность эмбеддера) и `bm25` — sparse, который Qdrant
считает сам (серверный инференс `qdrant/bm25`, модификатор `idf`). Поиск передаёт те же
`BM25_OPTIONS`, иначе термы запроса и документов не совпадут.

Поиск — один запрос Query API: prefetch dense и BM25 (IDF по чанкам тенанта,
`params.idf.corpus`), слияние RRF на стороне Qdrant. Фильтр тенанта — в каждом prefetch
и на верхнем уровне.

Замена документа: сначала upsert новых точек, затем удаление прочих точек документа —
документ не пропадает из поиска на время замены. Точки документа ищутся по
(`tenant_id`, `source_id`, `external_id`): ключ не меняется при пересоздании записи.
"""

from collections.abc import Collection, Sequence
from typing import Any
from uuid import UUID

import httpx

from app.modules.knowledge.domain.errors import VectorIndexError
from app.modules.knowledge.domain.indexing import ChunkHit, IndexedDocument
from app.modules.shared.kernel import TenantId

DENSE_VECTOR = "dense"
SPARSE_VECTOR = "bm25"
BM25_MODEL = "qdrant/bm25"
# Русский стемминг, стоп-слова русского и английского; avg_len — средняя длина чанка
# в словах (чанки 300-800 токенов, architecture.md §8).
BM25_OPTIONS: dict[str, Any] = {
    "stemmer": {"type": "snowball", "language": "russian"},
    "stopwords": {"languages": ["russian", "english"]},
    "avg_len": 300,
}

_PAYLOAD_INDEXES: dict[str, dict[str, Any]] = {
    "tenant_id": {"type": "keyword", "is_tenant": True},
    "source_id": {"type": "keyword"},
    "external_id": {"type": "keyword"},
}
_UPSERT_BATCH = 64
_DELETE_BATCH = 512
_PREFETCH_MIN = 20


def create_qdrant_index(
    url: str,
    *,
    collection: str,
    dimensions: int,
    api_key: str | None = None,
    timeout_s: float = 30.0,
) -> "QdrantChunkIndex":
    headers = {"api-key": api_key} if api_key else {}
    http = httpx.AsyncClient(base_url=url, headers=headers, timeout=timeout_s)
    return QdrantChunkIndex(http, collection=collection, dimensions=dimensions)


class QdrantChunkIndex:
    def __init__(self, http: httpx.AsyncClient, *, collection: str, dimensions: int) -> None:
        self.http = http
        self.collection = collection
        self.dimensions = dimensions

    async def ensure_collection(self) -> None:
        info = await self._collection_info()
        if info is None:
            created = await self._request(
                "PUT", "", json=self._collection_spec(), allow_statuses=(409,)
            )
            info = await self._collection_info()
            if info is None:
                raise VectorIndexError(f"коллекция {self.collection} не создана: {created}")
        self._check_params(info["config"]["params"])
        existing = info.get("payload_schema") or {}
        for field, schema in _PAYLOAD_INDEXES.items():
            if field not in existing:
                await self._request(
                    "PUT",
                    "/index",
                    json={"field_name": field, "field_schema": schema},
                    params={"wait": "true"},
                )

    async def replace_document(
        self, tenant_id: TenantId, source_id: UUID, document: IndexedDocument
    ) -> None:
        points = [
            {
                "id": str(chunk.chunk_id),
                "vector": {
                    DENSE_VECTOR: list(chunk.dense),
                    SPARSE_VECTOR: {
                        "text": chunk.text,
                        "model": BM25_MODEL,
                        "options": BM25_OPTIONS,
                    },
                },
                "payload": {
                    "tenant_id": str(tenant_id),
                    "source_id": str(source_id),
                    "document_id": str(document.document_id),
                    "external_id": document.external_id,
                    "title": document.title,
                    "url": document.url,
                    "section": chunk.section,
                    "ord": chunk.ord,
                    "text": chunk.text,
                },
            }
            for chunk in document.chunks
        ]
        for start in range(0, len(points), _UPSERT_BATCH):
            await self._request(
                "PUT",
                "/points",
                json={"points": points[start : start + _UPSERT_BATCH]},
                params={"wait": "true"},
            )
        stale: dict[str, Any] = {
            "must": [
                *_source_conditions(tenant_id, source_id),
                {"key": "external_id", "match": {"value": document.external_id}},
            ]
        }
        if points:
            stale["must_not"] = [{"has_id": [p["id"] for p in points]}]
        await self._delete(stale)

    async def delete_documents(
        self, tenant_id: TenantId, source_id: UUID, external_ids: Collection[str]
    ) -> None:
        ids = sorted(external_ids)
        for start in range(0, len(ids), _DELETE_BATCH):
            batch = ids[start : start + _DELETE_BATCH]
            await self._delete(
                {
                    "must": [
                        *_source_conditions(tenant_id, source_id),
                        {"key": "external_id", "match": {"any": batch}},
                    ]
                }
            )

    async def search(
        self, tenant_id: TenantId, text: str, dense: Sequence[float], limit: int
    ) -> list[ChunkHit]:
        tenant = {"must": [_tenant_condition(tenant_id)]}
        # Каждый ретривер отдаёт кандидатов с запасом: RRF выигрывает от глубины списков.
        depth = max(limit, _PREFETCH_MIN)
        result = await self._request(
            "POST",
            "/points/query",
            json={
                "prefetch": [
                    {"query": list(dense), "using": DENSE_VECTOR, "filter": tenant, "limit": depth},
                    {
                        "query": {"text": text, "model": BM25_MODEL, "options": BM25_OPTIONS},
                        "using": SPARSE_VECTOR,
                        "filter": tenant,
                        # IDF — по чанкам тенанта, а не всей коллекции (Qdrant ≥ 1.19).
                        "params": {"idf": {"corpus": tenant}},
                        "limit": depth,
                    },
                ],
                "query": {"fusion": "rrf"},
                "filter": tenant,
                "limit": limit,
                "with_payload": True,
            },
        )
        try:
            return [_hit(point) for point in result["points"]]
        except (KeyError, TypeError, ValueError) as error:
            raise VectorIndexError(f"Qdrant query: неожиданный ответ: {error!r}") from error

    async def aclose(self) -> None:
        await self.http.aclose()

    def _collection_spec(self) -> dict[str, Any]:
        return {
            "vectors": {DENSE_VECTOR: {"size": self.dimensions, "distance": "Cosine"}},
            "sparse_vectors": {SPARSE_VECTOR: {"modifier": "idf"}},
            "hnsw_config": {"payload_m": 16, "m": 0},
        }

    def _check_params(self, params: dict[str, Any]) -> None:
        dense = (params.get("vectors") or {}).get(DENSE_VECTOR) or {}
        sparse = (params.get("sparse_vectors") or {}).get(SPARSE_VECTOR) or {}
        if dense.get("size") != self.dimensions or sparse.get("modifier") != "idf":
            raise VectorIndexError(
                f"коллекция {self.collection} несовместима: dense={dense},"
                f" {SPARSE_VECTOR}={sparse}, ожидалась размерность {self.dimensions}"
            )

    async def _collection_info(self) -> dict[str, Any] | None:
        result = await self._request("GET", "", allow_statuses=(404,))
        return result if isinstance(result, dict) else None

    async def _delete(self, filter_: dict[str, Any]) -> None:
        await self._request(
            "POST", "/points/delete", json={"filter": filter_}, params={"wait": "true"}
        )

    async def _request(
        self,
        method: str,
        path: str,
        *,
        json: dict[str, Any] | None = None,
        params: dict[str, str] | None = None,
        allow_statuses: Sequence[int] = (),
    ) -> Any:
        """`result` ответа Qdrant; для статусов из `allow_statuses` — None."""
        url = f"/collections/{self.collection}{path}"
        try:
            response = await self.http.request(method, url, json=json, params=params)
        except httpx.HTTPError as error:
            raise VectorIndexError(
                f"Qdrant {method} {url}: {type(error).__name__}: {error}"
            ) from error
        if response.status_code in allow_statuses:
            return None
        if response.is_error:
            raise VectorIndexError(
                f"Qdrant {method} {url}: {response.status_code} {response.text[:500]}"
            )
        return response.json().get("result")


def _tenant_condition(tenant_id: TenantId) -> dict[str, Any]:
    return {"key": "tenant_id", "match": {"value": str(tenant_id)}}


def _source_conditions(tenant_id: TenantId, source_id: UUID) -> list[dict[str, Any]]:
    return [
        _tenant_condition(tenant_id),
        {"key": "source_id", "match": {"value": str(source_id)}},
    ]


def _hit(point: dict[str, Any]) -> ChunkHit:
    payload = point["payload"]
    return ChunkHit(
        chunk_id=UUID(point["id"]),
        document_id=UUID(payload["document_id"]),
        source_id=UUID(payload["source_id"]),
        title=payload["title"],
        text=payload["text"],
        score=float(point["score"]),
        url=payload.get("url"),
        section=payload.get("section"),
    )
