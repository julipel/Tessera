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
from app.modules.tools.application.forms import (
    CREATE_LEAD,
    SHOW_FORM,
    create_lead_tool,
    form_values_problem,
    show_form_tool,
)
from app.modules.tools.application.knowledge import SEARCH_KNOWLEDGE, search_knowledge_tool
from app.modules.tools.application.registry import ToolRegistry
from app.modules.tools.application.show_entities import SHOW_ENTITIES, show_entities_tool
from app.modules.tools.application.suggest_replies import SUGGEST_REPLIES, suggest_replies_tool
from app.modules.tools.domain.definition import (
    CANCEL_ACTION_ID,
    CONFIRM_ACTION_ID,
    ConfirmLabels,
    InvalidToolDefinitionError,
    ToolContext,
    ToolDefinition,
    ToolHandler,
    ToolInvocation,
)
from app.modules.tools.domain.ports import Catalog, KnowledgeSearcher, LeadStore
from app.modules.tools.domain.result import ToolError, ToolErrorCode, ToolResult

__all__ = [
    "CANCEL_ACTION_ID",
    "CONFIRM_ACTION_ID",
    "CREATE_LEAD",
    "GET_ENTITY",
    "SEARCH_CATALOG",
    "SEARCH_KNOWLEDGE",
    "SHOW_ENTITIES",
    "SHOW_FORM",
    "SUGGEST_REPLIES",
    "UPDATE_DIALOG_STATE",
    "Catalog",
    "ConfirmLabels",
    "InvalidToolDefinitionError",
    "KnowledgeSearcher",
    "LeadStore",
    "ToolContext",
    "ToolDefinition",
    "ToolError",
    "ToolErrorCode",
    "ToolHandler",
    "ToolInvocation",
    "ToolRegistry",
    "ToolResult",
    "builtin_tools",
    "create_lead_tool",
    "form_values_problem",
    "get_entity_tool",
    "search_catalog_tool",
    "search_knowledge_tool",
    "show_entities_tool",
    "show_form_tool",
    "suggest_replies_tool",
    "update_dialog_state_tool",
]
