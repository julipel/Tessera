"""Контракты API ↔ web, сгенерированные из packages/contracts/schemas (ADR-0005).

Импортировать типы отсюда, а не из `generated` напрямую: при перегенерации меняются
только внутренности `generated`, точка входа остаётся стабильной.
"""

from app.contracts.generated.agent_config_schema import AgentConfig
from app.contracts.generated.components_schema import Action, Component, FormField, Price
from app.contracts.generated.envelope_schema import Envelope
from app.contracts.generated.events_schema import (
    ComponentEvent,
    DoneEvent,
    ErrorEvent,
    Event,
    StatusEvent,
    SuggestionsEvent,
    TextDeltaEvent,
    TextDoneEvent,
    ToolFinishedEvent,
    ToolStartedEvent,
    TurnStartedEvent,
)
from app.contracts.generated.tools_schema import (
    HttpToolDefinition,
    ToolDefinition,
    ToolError,
    ToolResult,
)
from app.contracts.generated.user_input_schema import (
    ActionInput,
    FormSubmitInput,
    TextInput,
    UserInput,
)

__all__ = [
    "Action",
    "ActionInput",
    "AgentConfig",
    "Component",
    "ComponentEvent",
    "DoneEvent",
    "Envelope",
    "ErrorEvent",
    "Event",
    "FormField",
    "FormSubmitInput",
    "HttpToolDefinition",
    "Price",
    "StatusEvent",
    "SuggestionsEvent",
    "TextDeltaEvent",
    "TextDoneEvent",
    "TextInput",
    "ToolDefinition",
    "ToolError",
    "ToolFinishedEvent",
    "ToolResult",
    "ToolStartedEvent",
    "TurnStartedEvent",
    "UserInput",
]
