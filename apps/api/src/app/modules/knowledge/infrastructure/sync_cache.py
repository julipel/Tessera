"""Данные источника на время одной синхронизации (REVIEW.md: K-5, K-13).

`discover` кладёт сюда записи или страницы, `fetch` берёт их, не обращаясь к источнику на
каждый элемент. Вытеснения нет: у источника одна синхронизация за раз (`open_sync`), а
`run_sync` в конце (и при ошибке) вызывает `release` коннектора — данные не переживают её.
"""

from uuid import UUID

from app.modules.knowledge.domain.ingestion import SourceSpec
from app.modules.shared.kernel import TenantId


class SyncCache[T]:
    def __init__(self) -> None:
        self._items: dict[tuple[TenantId, UUID], T] = {}

    def get(self, source: SourceSpec) -> T | None:
        return self._items.get((source.tenant_id, source.source_id))

    def put(self, source: SourceSpec, value: T) -> T:
        self._items[(source.tenant_id, source.source_id)] = value
        return value

    def release(self, source: SourceSpec) -> None:
        self._items.pop((source.tenant_id, source.source_id), None)

    def __len__(self) -> int:
        return len(self._items)
