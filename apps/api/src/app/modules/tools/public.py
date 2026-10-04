"""Публичный интерфейс модуля tools — единственная точка входа для других модулей."""

from app.modules.tools.application.builtin import (
    UPDATE_DIALOG_STATE,
    builtin_tools,
    update_dialog_state_tool,
)
from app.modules.tools.application.catalog import (
    GET_ENTITY,
    SEARCH_CATALOG,
    get_entity_tool,
    search_catalog_tool,
)
from app.modules.tools.application.knowledge import SEARCH_KNOWLEDGE, search_knowledge_tool
from app.modules.tools.application.registry import ToolRegistry
from app.modules.tools.application.show_entities import SHOW_ENTITIES, show_entities_tool
from app.modules.tools.domain.definition import (
    InvalidToolDefinitionError,
    ToolContext,
    ToolDefinition,
    ToolHandler,
    ToolInvocation,
)
from app.modules.tools.domain.ports import Catalog, KnowledgeSearcher
from app.modules.tools.domain.result import ToolError, ToolErrorCode, ToolResult

__all__ = [
    "GET_ENTITY",
    "SEARCH_CATALOG",
    "SEARCH_KNOWLEDGE",
    "SHOW_ENTITIES",
    "UPDATE_DIALOG_STATE",
    "Catalog",
    "InvalidToolDefinitionError",
    "KnowledgeSearcher",
    "ToolContext",
    "ToolDefinition",
    "ToolError",
    "ToolErrorCode",
    "ToolHandler",
    "ToolInvocation",
    "ToolRegistry",
    "ToolResult",
    "builtin_tools",
    "get_entity_tool",
    "search_catalog_tool",
    "search_knowledge_tool",
    "show_entities_tool",
    "update_dialog_state_tool",
]
