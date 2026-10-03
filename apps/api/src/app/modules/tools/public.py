"""Публичный интерфейс модуля tools — единственная точка входа для других модулей."""

from app.modules.tools.application.builtin import (
    UPDATE_DIALOG_STATE,
    builtin_tools,
    update_dialog_state_tool,
)
from app.modules.tools.application.registry import ToolRegistry
from app.modules.tools.domain.definition import (
    InvalidToolDefinitionError,
    ToolContext,
    ToolDefinition,
    ToolHandler,
    ToolInvocation,
)
from app.modules.tools.domain.result import ToolError, ToolErrorCode, ToolResult

__all__ = [
    "UPDATE_DIALOG_STATE",
    "InvalidToolDefinitionError",
    "ToolContext",
    "ToolDefinition",
    "ToolError",
    "ToolErrorCode",
    "ToolHandler",
    "ToolInvocation",
    "ToolRegistry",
    "ToolResult",
    "builtin_tools",
    "update_dialog_state_tool",
]
