"""Tool Registry (architecture.md §5, §9): валидация аргументов по JSON Schema, параллельное
исполнение с таймаутами. Ошибки любого вызова — в `ToolResult.error`, а не исключением."""

import asyncio
import re
import time
from collections.abc import Iterable, Sequence

import structlog
from jsonschema import Draft202012Validator, SchemaError

from app.modules.tools.domain.definition import (
    InvalidToolDefinitionError,
    ToolContext,
    ToolDefinition,
    ToolInvocation,
)
from app.modules.tools.domain.result import ToolError, ToolErrorCode, ToolResult

logger = structlog.get_logger(__name__)

_NAME = re.compile(r"^[a-z][a-z0-9_]*$")


class ToolRegistry:
    """Инструменты хода. Конфигурация проверяется при создании: невалидная схема или дубль
    имени — ошибка настройки тенанта, а не шага модели."""

    def __init__(self, definitions: Iterable[ToolDefinition]) -> None:
        self._tools: dict[str, tuple[ToolDefinition, Draft202012Validator]] = {}
        for definition in definitions:
            name = definition.name
            if not _NAME.fullmatch(name):
                raise InvalidToolDefinitionError(f"{name!r}: имя инструмента — snake_case")
            if name in self._tools:
                raise InvalidToolDefinitionError(f"{name!r}: инструмент объявлен дважды")
            if definition.timeout_s <= 0:
                raise InvalidToolDefinitionError(f"{name!r}: timeout_s должен быть > 0")
            try:
                Draft202012Validator.check_schema(definition.parameters)
            except SchemaError as e:
                raise InvalidToolDefinitionError(
                    f"{name!r}: невалидная JSON Schema аргументов: {e.message}"
                ) from e
            if definition.parameters.get("type") != "object":
                raise InvalidToolDefinitionError(f"{name!r}: схема аргументов — type: object")
            self._tools[name] = (definition, Draft202012Validator(definition.parameters))

    def definitions(self) -> tuple[ToolDefinition, ...]:
        return tuple(definition for definition, _ in self._tools.values())

    async def execute_many(
        self, invocations: Sequence[ToolInvocation], ctx: ToolContext
    ) -> list[ToolResult]:
        """Параллельно; результаты в порядке `invocations`. Отмена отменяет все вызовы."""
        async with asyncio.TaskGroup() as group:
            tasks = [group.create_task(self._execute(inv, ctx)) for inv in invocations]
        return [task.result() for task in tasks]

    async def _execute(self, invocation: ToolInvocation, ctx: ToolContext) -> ToolResult:
        started = time.perf_counter()
        result = await self._run(invocation, ctx)
        logger.info(
            "tool.executed",
            tenant_id=str(ctx.tenant_id),
            conversation_id=str(ctx.conversation_id),
            turn_id=str(ctx.turn_id),
            tool=invocation.name,
            tool_call_id=invocation.id,
            duration_ms=round((time.perf_counter() - started) * 1000),
            error_code=result.error.code if result.error else None,
        )
        return result

    async def _run(self, invocation: ToolInvocation, ctx: ToolContext) -> ToolResult:
        entry = self._tools.get(invocation.name)
        if entry is None:
            available = ", ".join(self._tools) or "нет"
            return _failure(
                "validation_error",
                f"неизвестный инструмент {invocation.name!r}; доступны: {available}",
            )
        definition, validator = entry

        errors = sorted(validator.iter_errors(invocation.arguments), key=lambda e: e.json_path)
        if errors:
            details = "; ".join(f"{e.json_path}: {e.message}" for e in errors)
            return _failure(
                "validation_error",
                f"{invocation.name}: аргументы не соответствуют схеме: {details}",
            )

        try:
            async with asyncio.timeout(definition.timeout_s) as deadline:
                return await definition.handler(invocation.arguments, ctx)
        except TimeoutError:
            if not deadline.expired():  # таймаут изнутри обработчика — сбой обработчика
                return _crashed(invocation, ctx)
            return _failure(
                "timeout",
                f"{invocation.name}: нет ответа за {definition.timeout_s:g} с",
                retryable=True,
            )
        except Exception:
            return _crashed(invocation, ctx)


def _crashed(invocation: ToolInvocation, ctx: ToolContext) -> ToolResult:
    """Исключение обработчика: подробности — в лог, модели — без внутренностей."""
    logger.exception(
        "tool.failed",
        tenant_id=str(ctx.tenant_id),
        conversation_id=str(ctx.conversation_id),
        turn_id=str(ctx.turn_id),
        tool=invocation.name,
        tool_call_id=invocation.id,
    )
    return _failure(
        "upstream_error", f"{invocation.name}: данные временно недоступны", retryable=True
    )


def _failure(code: ToolErrorCode, message: str, *, retryable: bool = False) -> ToolResult:
    return ToolResult(error=ToolError(code=code, message=message, retryable=retryable))
