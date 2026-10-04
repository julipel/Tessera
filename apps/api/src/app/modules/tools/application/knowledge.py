"""Встроенный инструмент `search_knowledge` (contracts.md §4): поиск по знаниям тенанта через
порт `KnowledgeSearcher` (реализация — knowledge `KnowledgeSearch`) → фрагменты для модели
+ компонент `sources`.

`top_k` по умолчанию и `rerank` — из `knowledge.search_knowledge` конфига. В `sources` —
только фрагменты с url (без дублей по url): `SourceItem.url` в контракте обязателен.
"""

import re
from collections.abc import Mapping
from typing import Any

import structlog

from app.contracts import KnowledgeSearchConfig, SourceItem, Sources
from app.modules.knowledge.kernel import ChunkHit, EmbeddingError, VectorIndexError
from app.modules.tools.domain.definition import ToolContext, ToolDefinition, ToolHandler
from app.modules.tools.domain.ports import KnowledgeSearcher
from app.modules.tools.domain.result import ToolError, ToolResult

logger = structlog.get_logger(__name__)

SEARCH_KNOWLEDGE = "search_knowledge"
TOP_K_MAX = 10
SNIPPET_CHARS = 200
DEFAULT_TOP_K = 6

_DESCRIPTION = (
    "Найди ответ в базе знаний компании: условия доставки, оплаты, возврата, гарантии, "
    "описания услуг и товаров, правила, контакты. Вызывай перед ответом на такие вопросы. "
    "query — самостоятельная формулировка вопроса или ключевые слова, без местоимений и "
    "ссылок на прошлые реплики. Отвечай только по найденным фрагментам; если ничего "
    "подходящего нет — честно скажи, что ответа нет, не придумывай."
)
_WHITESPACE = re.compile(r"\s+")


def search_knowledge_tool(
    search: KnowledgeSearcher, settings: KnowledgeSearchConfig | None = None
) -> ToolDefinition:
    settings = settings or KnowledgeSearchConfig()
    default_top_k = settings.top_k or DEFAULT_TOP_K
    rerank = True if settings.rerank is None else settings.rerank
    return ToolDefinition(
        name=SEARCH_KNOWLEDGE,
        description=_DESCRIPTION,
        parameters={
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "minLength": 1,
                    "description": "Что искать: вопрос или ключевые слова.",
                },
                "top_k": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": max(TOP_K_MAX, default_top_k),
                    "description": f"Сколько фрагментов вернуть (по умолчанию {default_top_k}).",
                },
            },
            "required": ["query"],
            "additionalProperties": False,
        },
        handler=_handler(search, default_top_k, rerank),
        display_label="Ищу в базе знаний",
    )


def _handler(search: KnowledgeSearcher, default_top_k: int, rerank: bool) -> ToolHandler:
    async def handle(arguments: Mapping[str, Any], ctx: ToolContext, /) -> ToolResult:
        query: str = arguments["query"]
        top_k: int = arguments.get("top_k", default_top_k)
        try:
            hits = await search.search(ctx.tenant_id, query, top_k=top_k, rerank=rerank)
        except (EmbeddingError, VectorIndexError) as error:
            logger.warning(
                "knowledge.search_failed",
                tenant_id=str(ctx.tenant_id),
                conversation_id=str(ctx.conversation_id),
                turn_id=str(ctx.turn_id),
                error=str(error),
            )
            return ToolResult(
                error=ToolError(
                    code="upstream_error",
                    message=f"{SEARCH_KNOWLEDGE}: база знаний временно недоступна",
                    retryable=True,
                )
            )
        if not hits:
            return ToolResult(content={"fragments": [], "note": "в базе знаний ничего не найдено"})
        sources = sources_component(hits)
        return ToolResult(
            content={"fragments": [_fragment(n, hit) for n, hit in enumerate(hits, 1)]},
            components=(sources,) if sources else (),
        )

    return handle


def sources_component(hits: list[ChunkHit]) -> dict[str, Any] | None:
    items: list[SourceItem] = []
    seen: set[str] = set()
    for hit in hits:
        if hit.url and hit.url not in seen:
            seen.add(hit.url)
            items.append(SourceItem(title=hit.title, url=hit.url, snippet=_snippet(hit.text)))
    if not items:
        return None
    return Sources(type="sources", items=items).model_dump(mode="json")


def _fragment(n: int, hit: ChunkHit) -> dict[str, Any]:
    fragment: dict[str, Any] = {"n": n, "title": hit.title}
    if hit.section:
        fragment["section"] = hit.section
    if hit.url:
        fragment["url"] = hit.url
    fragment["text"] = hit.text
    return fragment


def _snippet(text: str) -> str:
    flat = _WHITESPACE.sub(" ", text).strip()
    if len(flat) <= SNIPPET_CHARS:
        return flat
    return flat[: SNIPPET_CHARS - 1].rstrip() + "…"
