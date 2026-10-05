"""Порт `ToolExecutor` поверх Tool Registry: перевод типов agent ↔ tools (architecture.md §5)."""

from collections.abc import Sequence

from app.modules.agent.domain.llm import ToolCall, ToolSchema
from app.modules.agent.domain.turn import TurnContext
from app.modules.tools.public import ToolContext, ToolInvocation, ToolRegistry, ToolResult


class RegistryToolExecutor:
    def __init__(self, registry: ToolRegistry) -> None:
        self._registry = registry
        self._schemas = tuple(
            ToolSchema(name=d.name, description=d.description, parameters=d.parameters)
            for d in registry.definitions()
        )
        self._labels = {d.name: d.display_label for d in registry.definitions()}

    def schemas(self) -> tuple[ToolSchema, ...]:
        return self._schemas

    def display_label(self, name: str) -> str | None:
        return self._labels.get(name)

    async def execute_many(
        self, calls: Sequence[ToolCall], ctx: TurnContext
    ) -> Sequence[ToolResult]:
        """Аргументы уже разобраны: вызовы с `arguments is None` цикл сюда не передаёт."""
        return await self._registry.execute_many(
            [_invocation(call) for call in calls], _tool_context(ctx)
        )

    async def execute_confirmed(self, call: ToolCall, ctx: TurnContext) -> ToolResult:
        return await self._registry.execute_confirmed(_invocation(call), _tool_context(ctx))


def _invocation(call: ToolCall) -> ToolInvocation:
    return ToolInvocation(id=call.id, name=call.name, arguments=call.arguments or {})


def _tool_context(ctx: TurnContext) -> ToolContext:
    return ToolContext(
        tenant_id=ctx.tenant_id, conversation_id=ctx.conversation_id, turn_id=ctx.turn_id
    )
