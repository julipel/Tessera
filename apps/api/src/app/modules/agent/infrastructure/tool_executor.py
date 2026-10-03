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

    def schemas(self) -> tuple[ToolSchema, ...]:
        return self._schemas

    async def execute_many(
        self, calls: Sequence[ToolCall], ctx: TurnContext
    ) -> Sequence[ToolResult]:
        """Аргументы уже разобраны: вызовы с `arguments is None` цикл сюда не передаёт."""
        invocations = [
            ToolInvocation(id=call.id, name=call.name, arguments=call.arguments or {})
            for call in calls
        ]
        return await self._registry.execute_many(
            invocations,
            ToolContext(
                tenant_id=ctx.tenant_id, conversation_id=ctx.conversation_id, turn_id=ctx.turn_id
            ),
        )
