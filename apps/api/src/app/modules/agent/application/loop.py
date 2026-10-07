"""Агентный цикл (architecture.md §5); поведение задают tests/test_agent_loop.py."""

import json
import time
from collections.abc import AsyncIterator, Iterator, Sequence
from dataclasses import dataclass, replace

import structlog

from app.modules.agent.domain.events import (
    AgentEvent,
    AnswerDelta,
    ComponentEmitted,
    DialogStateUpdated,
    FinishReason,
    SuggestionsOffered,
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
    UserMessage,
)
from app.modules.agent.domain.turn import ToolExecutor, TurnContext
from app.modules.memory.kernel import DialogState
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
        state = ctx.state
        visible = False  # клиент уже видит текст или компонент этого хода
        if ctx.confirmation is not None:
            outcome = _Outcome()
            async for event in self._resolve_confirmation(ctx, outcome):
                visible = visible or isinstance(event, ComponentEmitted)
                yield event
            if outcome.state is not None:
                state = outcome.state
            messages = _with_note(messages, outcome.note)
        retries = 0
        empty_retried = False
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
                        visible = visible or bool(text.strip())
                        yield AnswerDelta(text)
                    case ToolCallStarted(id=call_id, name=name):
                        yield ToolStarted(call_id, name, self._tools.display_label(name))
                    case ResponseCompleted(response=completed):
                        response = completed
            if response is None:
                raise RuntimeError("LLMClient: стрим завершился без ResponseCompleted")
            usage = _add(usage, response.usage)

            if not response.tool_calls and not visible:
                # Пустой ответ хода — один повтор шага, затем fallback-фраза (architecture.md
                # §12). Пустой последний шаг после текста или компонентов — обычный конец хода.
                logger.warning("agent.empty_response", turn_id=str(ctx.turn_id), step=steps)
                if empty_retried:
                    finish = FinishReason.EMPTY_RESPONSE
                    break
                empty_retried = True
                continue

            if not response.tool_calls:
                finish = FinishReason.ANSWERED
                break

            started = time.perf_counter()
            # Состояние шага, а не начала хода: сценарий, сменённый на прошлом шаге, уже
            # действует для слотов (ADR-0026).
            results = await self._execute(response.tool_calls, replace(ctx, state=state))
            batch_ms = round((time.perf_counter() - started) * 1000)
            for call, result in zip(response.tool_calls, results, strict=True):
                for event in _reported(call, result, batch_ms):
                    visible = visible or isinstance(event, ComponentEmitted)
                    yield event
            patches = [r.state_patch for r in results if r.ok and r.state_patch]
            if patches:
                for patch in patches:
                    state = state.apply(patch)
                yield DialogStateUpdated(state)
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

    async def _resolve_confirmation(
        self, ctx: TurnContext, outcome: "_Outcome"
    ) -> AsyncIterator[AgentEvent]:
        """Ответ на подтверждение — до модели и без неё (ADR-0021): подтверждённый вызов
        исполняется с аргументами из состояния, ожидание снимается. Итог — пометка
        к сообщению пользователя, чтобы модель знала, что произошло."""
        assert ctx.confirmation is not None
        pending = ctx.state.pending_confirmation
        if pending is None or pending.confirm_id != ctx.confirmation.confirm_id:
            outcome.note = "[Подтверждение устарело: действие не выполнено]"
            return
        state = ctx.state.apply({"pending_confirmation": None})
        if not ctx.confirmation.approved:
            outcome.note = f"[Пользователь отменил {pending.tool}: действие не выполнено]"
        else:
            arguments = dict(pending.arguments)
            call = ToolCall(
                id=pending.confirm_id,
                name=pending.tool,
                arguments=arguments,
                raw_arguments=json.dumps(arguments, ensure_ascii=False),
            )
            yield ToolStarted(call.id, call.name, self._tools.display_label(call.name))
            started = time.perf_counter()
            result = await self._tools.execute_confirmed(call, ctx)
            for event in _reported(call, result, round((time.perf_counter() - started) * 1000)):
                yield event
            if result.ok and result.state_patch:
                state = state.apply(result.state_patch)
            outcome.note = f"[Подтверждено, {pending.tool}: {_as_message(call, result).content}]"
        outcome.state = state
        yield DialogStateUpdated(state)

    async def _execute(self, calls: Sequence[ToolCall], ctx: TurnContext) -> list[ToolResult]:
        """Результаты в порядке `calls`; вызовы с неразобранными аргументами отклоняются
        без исполнителя."""
        valid = [c for c in calls if c.arguments is not None]
        executed = iter(await self._tools.execute_many(valid, ctx) if valid else ())
        return [
            next(executed) if call.arguments is not None else _unparseable(call) for call in calls
        ]


@dataclass(slots=True)
class _Outcome:
    """Итог разбора подтверждения: пометка для модели и новое состояние (None — не менялось)."""

    note: str = ""
    state: DialogState | None = None


def _with_note(messages: tuple[LLMMessage, ...], note: str) -> tuple[LLMMessage, ...]:
    """Пометка дописывается к последнему сообщению пользователя (ввод этого хода)."""
    if messages and isinstance(last := messages[-1], UserMessage):
        return (*messages[:-1], UserMessage(f"{last.text}\n{note}"))
    return (*messages, UserMessage(note))


def _reported(call: ToolCall, result: ToolResult, duration_ms: int) -> Iterator[AgentEvent]:
    """События завершённого вызова: итог, UI-компоненты и быстрые ответы."""
    yield ToolFinished(
        tool_call_id=call.id,
        name=call.name,
        ok=result.ok,
        error_code=result.error.code if result.error else None,
        arguments=call.raw_arguments if call.arguments is None else call.arguments,
        content=result.content,
        error_message=result.error.message if result.error else None,
        duration_ms=duration_ms if call.arguments is not None else 0,
    )
    for component in result.components:
        yield ComponentEmitted(component)
    if result.ok and result.suggestions:
        yield SuggestionsOffered(result.suggestions)


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
