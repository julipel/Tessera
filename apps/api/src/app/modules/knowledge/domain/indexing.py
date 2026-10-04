"""Что пайплайн кладёт в векторный индекс: чанки документа с dense-векторами (§8).

Sparse-вектор (BM25) считает сам индекс по тексту чанка.
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
