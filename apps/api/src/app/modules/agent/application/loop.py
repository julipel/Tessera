"""Агентный цикл (architecture.md §5); поведение задают tests/test_agent_loop.py."""

import json
import time
from collections.abc import AsyncIterator, Sequence

import structlog

from app.modules.agent.domain.events import (
    AgentEvent,
    AnswerDelta,
    ComponentEmitted,
    FinishReason,
    ToolFinished,
    ToolStarted,
    TurnCompleted,
)
from app.modules.agent.domain.llm import (
    LLMClient,
    LLMMessage,
    LLMRequest,
    LLMResponse,
    ResponseCompleted,
    TextDelta,
    ToolCall,
    ToolCallStarted,
    ToolResultMessage,
    Usage,
)
from app.modules.agent.domain.turn import ToolExecutor, TurnContext
from app.modules.tools.public import ToolError, ToolResult

logger = structlog.get_logger(__name__)


class AgentLoop:
    def __init__(self, llm: LLMClient, tools: ToolExecutor) -> None:
        self._llm = llm
        self._tools = tools

    async def run_turn(self, ctx: TurnContext) -> AsyncIterator[AgentEvent]:
        """Ход агента: шаги модели и инструментов до ответа или мягкого завершения.

        Отмена хода (CancelledError) не перехватывается — она прерывает стрим модели
        или исполнение инструментов."""
        messages: tuple[LLMMessage, ...] = ctx.history
        schemas = self._tools.schemas()
        usage = Usage()
        retries = 0
        steps = 0
        finish = FinishReason.STEP_LIMIT

        while steps < ctx.limits.max_steps:
            steps += 1
            request = LLMRequest(
                model=ctx.model,
                system=ctx.system,
                messages=messages,
                tools=schemas,
                temperature=ctx.temperature,
            )
            response: LLMResponse | None = None
            async for chunk in self._llm.stream(request):
                match chunk:
                    case TextDelta(text=text):
                        yield AnswerDelta(text)
                    case ToolCallStarted(id=call_id, name=name):
                        yield ToolStarted(tool_call_id=call_id, name=name)
                    case ResponseCompleted(response=completed):
                        response = completed
            if response is None:
                raise RuntimeError("LLMClient: стрим завершился без ResponseCompleted")
            usage = _add(usage, response.usage)

            if not response.tool_calls:
                finish = FinishReason.ANSWERED
                break

            started = time.perf_counter()
            results = await self._execute(response.tool_calls, ctx)
            batch_ms = round((time.perf_counter() - started) * 1000)
            for call, result in zip(response.tool_calls, results, strict=True):
                yield ToolFinished(
                    tool_call_id=call.id,
                    name=call.name,
                    ok=result.ok,
                    error_code=result.error.code if result.error else None,
                    arguments=call.raw_arguments if call.arguments is None else call.arguments,
                    content=result.content,
                    error_message=result.error.message if result.error else None,
                    duration_ms=batch_ms if call.arguments is not None else 0,
                )
                for component in result.components:
                    yield ComponentEmitted(component)
            messages = (
                *messages,
                response.as_message(),
                *(_as_message(c, r) for c, r in zip(response.tool_calls, results, strict=True)),
            )

            if any(r.error and r.error.code == "validation_error" for r in results):
                retries += 1
                if retries > ctx.limits.max_tool_retries:
                    finish = FinishReason.TOOL_RETRIES_EXHAUSTED
                    break

        if finish is not FinishReason.ANSWERED:
            yield AnswerDelta(ctx.fallback_message)
        logger.info(
            "agent.turn_completed",
            tenant_id=str(ctx.tenant_id),
            conversation_id=str(ctx.conversation_id),
            turn_id=str(ctx.turn_id),
            finish=finish.value,
            steps=steps,
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
        )
        yield TurnCompleted(finish=finish, usage=usage, steps=steps)

    async def _execute(self, calls: Sequence[ToolCall], ctx: TurnContext) -> list[ToolResult]:
        """Результаты в порядке `calls`; вызовы с неразобранными аргументами отклоняются
        без исполнителя."""
        valid = [c for c in calls if c.arguments is not None]
        executed = iter(await self._tools.execute_many(valid, ctx) if valid else ())
        return [
            next(executed) if call.arguments is not None else _unparseable(call) for call in calls
        ]


def _unparseable(call: ToolCall) -> ToolResult:
    return ToolResult(
        error=ToolError(
            code="validation_error",
            message=f"{call.name}: аргументы должны быть JSON-объектом, получено: "
            f"{call.raw_arguments!r}",
            retryable=False,
        )
    )


def _as_message(call: ToolCall, result: ToolResult) -> ToolResultMessage:
    if result.error is not None:
        return ToolResultMessage(
            tool_call_id=call.id,
            content=f"{result.error.code}: {result.error.message}",
            is_error=True,
        )
    content = result.content
    return ToolResultMessage(
        tool_call_id=call.id,
        content=content if isinstance(content, str) else json.dumps(content, ensure_ascii=False),
    )


def _add(a: Usage, b: Usage) -> Usage:
    return Usage(a.input_tokens + b.input_tokens, a.output_tokens + b.output_tokens)
