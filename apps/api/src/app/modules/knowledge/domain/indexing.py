"""Что пайплайн кладёт в векторный индекс (чанки документа с dense-векторами) и что поиск
из него достаёт (§8).

Sparse-вектор (BM25) считает сам индекс по тексту чанка и запроса.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from uuid import UUID


@dataclass(frozen=True, slots=True)
class IndexedChunk:
    """Чанк для индекса; `chunk_id` совпадает с `Chunk.id` в Postgres и с id точки."""

    chunk_id: UUID
    ord: int
    text: str
    dense: Sequence[float]
    section: str | None = None


@dataclass(frozen=True, slots=True)
class IndexedDocument:
    """Документ источника целиком: его точки в индексе заменяются этим набором чанков."""

    document_id: UUID
    external_id: str
    title: str
    chunks: Sequence[IndexedChunk]
    url: str | None = None


@dataclass(frozen=True, slots=True)
class ChunkHit:
    """Найденный чанк: payload точки индекса и оценка RRF (сравнима только внутри одной
    выдачи)."""

    chunk_id: UUID
    document_id: UUID
    source_id: UUID
    title: str
    text: str
    score: float
    url: str | None = None
    section: str | None = None
