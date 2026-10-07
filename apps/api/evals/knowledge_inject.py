"""Фрагменты базы знаний, подмешанные в выдачу `search_knowledge` одного диалога эвала
(`knowledge_inject`, ADR-0031): проверка устойчивости к prompt injection в данных источников
без правки данных тенанта.

Фрагменты диалога — в contextvar: диалоги идут параллельно в своих задачах, инструменты хода
наследуют контекст задачи диалога.
"""

from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from contextvars import ContextVar
from uuid import UUID, uuid5

from app.modules.knowledge.public import ChunkHit
from app.modules.shared.kernel import TenantId
from app.modules.tools.public import KnowledgeSearcher
from evals.dialogs import InjectedFragment

_INJECTED: ContextVar[tuple[ChunkHit, ...]] = ContextVar("eval_knowledge_inject", default=())
_NAMESPACE = UUID("3b0f8a4e-5d2c-4f61-9e7a-1c8d2b6f4a90")


@contextmanager
def injected(fragments: Sequence[InjectedFragment]) -> Iterator[None]:
    """Подмешивать `fragments` в выдачу поиска внутри блока (диалога)."""
    hits = tuple(
        ChunkHit(
            chunk_id=uuid5(_NAMESPACE, f"chunk:{n}:{f.title}"),
            document_id=uuid5(_NAMESPACE, f"doc:{f.title}"),
            source_id=uuid5(_NAMESPACE, "source"),
            title=f.title,
            text=f.text,
            score=1.0,
            url=f.url,
        )
        for n, f in enumerate(fragments)
    )
    token = _INJECTED.set(hits)
    try:
        yield
    finally:
        _INJECTED.reset(token)


class InjectingKnowledge:
    """Поиск по знаниям: подмешанные фрагменты диалога — первыми, затем выдача `inner`
    (всего не больше `top_k`)."""

    def __init__(self, inner: KnowledgeSearcher) -> None:
        self._inner = inner

    async def search(
        self, tenant_id: TenantId, query: str, *, top_k: int, rerank: bool
    ) -> list[ChunkHit]:
        hits = list(_INJECTED.get())
        found = await self._inner.search(tenant_id, query, top_k=top_k, rerank=rerank)
        return [*hits, *found][: max(top_k, len(hits))]
