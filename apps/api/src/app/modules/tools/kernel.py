"""Типы tools без фреймворков — для domain-слоёв других модулей (ADR-0007, ADR-0008).

`public.py` экспортирует ещё и `ToolRegistry` (structlog, jsonschema), поэтому domain-слой
agent импортирует типы отсюда.
"""

from app.modules.tools.domain.definition import ToolContext, ToolInvocation
from app.modules.tools.domain.result import ToolError, ToolErrorCode, ToolResult

__all__ = ["ToolContext", "ToolError", "ToolErrorCode", "ToolInvocation", "ToolResult"]
