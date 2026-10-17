"""Агентный цикл (architecture.md §5); поведение задают tests/test_agent_loop.py."""

import asyncio
import json
import time
from collections.abc import AsyncIterator, Awaitable, Iterator, Sequence
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
    AssistantMessage,
    LLMClient,
    LLMError,
    LLMMessage,
    LLMRequest,
    LLMResponse,
    ResponseCompleted,
    StopReason,
    TextDelta,
    ToolCall,
    ToolCallStarted,
    ToolResultMessage,
    Usage,
    UserMessage,
)
from app.modules.agent.domain.turn import ToolExecutor, TurnContext, TurnTimeoutError
from app.modules.memory.kernel import DialogState
from app.modules.tools.public import (
    ToolError,
    ToolResult,
    asks_confirmation,
    confirmation_already_asked,
)

# Сколько символов неразобранных аргументов вызова уходит модели и в журнал (AG-11).
RAW_ARGUMENTS_CHARS = 500
logger = structlog.get_logger(__name__)


class AgentLoop:
    def __init__(self, llm: LLMClient, tools: ToolExecutor) -> None:
        self._llm = llm
        self._tools = tools

    async def run_turn(self, ctx: TurnContext) -> AsyncIterator[AgentEvent]:
        """Ход агента: шаги модели и инструментов до ответа или мягкого завершения.

        Отмена хода (CancelledError) не перехватывается — она прерывает стрим модели
        или исполнение инструментов. Дедлайн `turn_timeout_s` прерывает их так же и
        поднимает `TurnTimeoutError`; отданные до него события остаются у клиента."""
        deadline = asyncio.get_running_loop().time() + ctx.limits.turn_timeout_s
        messages: tuple[LLMMessage, ...] = ctx.history
        schemas = self._tools.schemas()
        usage = Usage()
        state = ctx.state
        visible = False  # клиент уже видит текст или компонент этого хода
        if ctx.confirmation is not None:
            outcome = _Outcome()
            async for event in self._resolve_confirmation(ctx, outcome, deadline):
                visible = visible or isinstance(event, ComponentEmitted)
                yield event
            if outcome.state is not None:
                state = outcome.state
            messages = _with_note(messages, outcome.note)
        retries = 0
        empty_retried = False
        confirmation_asked = False
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
                max_output_tokens=ctx.limits.max_output_tokens,
            )
            response: LLMResponse | None = None
            stream = self._llm.stream(request)
            # Дедлайн — на каждое ожидание чанка, а не на блок с yield: таймаут в async-
            # генераторе не должен охватывать код потребителя.
            while (chunk := await _before(deadline, anext(stream, None), ctx)) is not None:
                match chunk:
                    case TextDelta(text=text):
                        visible = visible or bool(text.strip())
                        yield AnswerDelta(text)
                    case ToolCallStarted(id=call_id, name=name):
                        yield ToolStarted(call_id, name, self._tools.display_label(name))
                    case ResponseCompleted(response=completed):
                        response = completed
            if response is None:
                # Нарушение контракта клиента (ошибка адаптера, не провайдера): в лог
                # отдельно, клиенту — «Повторить» вместо internal (REVIEW.md: AG-13).
                # FallbackLLM это не повторяет: для него стрим закончился штатно.
                logger.error(
                    "agent.llm_contract_violation",
                    tenant_id=str(ctx.tenant_id),
                    conversation_id=str(ctx.conversation_id),
                    turn_id=str(ctx.turn_id),
                    step=steps,
                )
                raise LLMError("LLMClient: стрим завершился без ResponseCompleted", retryable=True)
            usage = _add(usage, response.usage)
            if response.stop_reason is StopReason.MAX_TOKENS:
                # Текст шага уже у клиента — ответ не отзываем, только отмечаем обрезку.
                logger.warning(
                    "agent.output_truncated",
                    tenant_id=str(ctx.tenant_id),
                    conversation_id=str(ctx.conversation_id),
                    turn_id=str(ctx.turn_id),
                    step=steps,
                    max_output_tokens=ctx.limits.max_output_tokens,
                )

            if not response.tool_calls and not visible:
                # Пустой ответ хода — один повтор шага, затем fallback-фраза (architecture.md
                # §12). Пустой последний шаг после текста или компонентов — обычный конец хода.
                # Пустой из-за лимита вывода (рассуждения reasoning-модели съели весь
                # max_output_tokens) не повторяется: с тем же лимитом повтор тоже пуст (ADR-0042).
                logger.warning(
                    "agent.empty_response",
                    tenant_id=str(ctx.tenant_id),
                    conversation_id=str(ctx.conversation_id),
                    turn_id=str(ctx.turn_id),
                    step=steps,
                    stop_reason=response.stop_reason.value,
                )
                if empty_retried or response.stop_reason is StopReason.MAX_TOKENS:
                    finish = FinishReason.EMPTY_RESPONSE
                    break
                empty_retried = True
                continue

            if not response.tool_calls:
                finish = FinishReason.ANSWERED
                break

            used = usage.input_tokens + usage.output_tokens
            if used > ctx.limits.max_turn_tokens:
                # Бюджет хода исчерпан (ADR-0042): вызовы шага не исполняются, следующего
                # шага нет — мягкое завершение, как при лимите шагов.
                logger.warning(
                    "agent.turn_budget_exhausted",
                    tenant_id=str(ctx.tenant_id),
                    conversation_id=str(ctx.conversation_id),
                    turn_id=str(ctx.turn_id),
                    step=steps,
                    used_tokens=used,
                    max_turn_tokens=ctx.limits.max_turn_tokens,
                )
                # Индикатор вызовов уже показан (ToolStarted из стрима) — закрываем каждый,
                # чтобы журнал и SSE не остались с висячим tool_started.
                for call in response.tool_calls:
                    for event in _reported(call, _over_budget(call), 0):
                        yield event
                finish = FinishReason.STEP_LIMIT
                break

            started = time.perf_counter()
            # Состояние шага, а не начала хода: сценарий, сменённый на прошлом шаге, уже
            # действует для слотов (ADR-0026). Вызовы сверх лимита шага не исполняются.
            limit = ctx.limits.max_tool_calls_per_step
            executed = await _before(
                deadline,
                self._execute(response.tool_calls[:limit], replace(ctx, state=state)),
                ctx,
            )
            results = [*executed, *(_over_limit(c, limit) for c in response.tool_calls[limit:])]
            # Одно подтверждение за ход (REVIEW.md: AG-12): ожидание в состоянии одно, и
            # кнопка подтверждения прошлого шага стала бы «устаревшей».
            for i, (call, result) in enumerate(zip(response.tool_calls, results, strict=True)):
                if asks_confirmation(result):
                    if confirmation_asked:
                        results[i] = confirmation_already_asked(call.name)
                    confirmation_asked = True
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
                _clipped_message(response),
                *(_as_message(c, r) for c, r in zip(response.tool_calls, results, strict=True)),
            )

            # Отказ по лимиту вызовов — не ошибка аргументов, ретраи на него не тратятся.
            if any(r.error and r.error.code == "validation_error" for r in executed):
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
        self, ctx: TurnContext, outcome: "_Outcome", deadline: float
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
            result = await _before(deadline, self._tools.execute_confirmed(call, ctx), ctx)
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


async def _before[T](deadline: float, awaitable: Awaitable[T], ctx: TurnContext) -> T:
    """Дождаться `awaitable` до дедлайна хода; не успел — `TurnTimeoutError`. TimeoutError
    изнутри (таймаут клиента, инструмента) — не дедлайн хода и пробрасывается как есть."""
    try:
        async with asyncio.timeout_at(deadline) as timeout:
            return await awaitable
    except TimeoutError as e:
        if not timeout.expired():
            raise
        logger.warning(
            "agent.turn_timeout",
            tenant_id=str(ctx.tenant_id),
            conversation_id=str(ctx.conversation_id),
            turn_id=str(ctx.turn_id),
            turn_timeout_s=ctx.limits.turn_timeout_s,
        )
        raise TurnTimeoutError(f"ход не уложился в {ctx.limits.turn_timeout_s:g} с") from e


def _over_limit(call: ToolCall, limit: int) -> ToolResult:
    return ToolResult(
        error=ToolError(
            code="validation_error",
            message=f"{call.name}: за шаг исполняется не больше {limit} вызовов, этот не "
            "исполнен; если он нужен, вызови его следующим шагом",
            retryable=False,
        )
    )


def _over_budget(call: ToolCall) -> ToolResult:
    return ToolResult(
        error=ToolError(
            code="validation_error",
            message=f"{call.name}: не исполнен — исчерпан бюджет токенов хода",
            retryable=False,
        )
    )


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
        arguments=_clipped(call.raw_arguments) if call.arguments is None else call.arguments,
        content=result.content,
        error_message=result.error.message if result.error else None,
        duration_ms=duration_ms if call.arguments is not None else 0,
    )
    for component in result.components:
        yield ComponentEmitted(component)
    if result.ok and result.suggestions:
        yield SuggestionsOffered(result.suggestions)


def _clipped_message(response: LLMResponse) -> AssistantMessage:
    """Ответ шага для следующих шагов: неразобранные аргументы — обрезанными (AG-11)."""
    message = response.as_message()
    if all(c.arguments is not None for c in message.tool_calls):
        return message
    calls = tuple(
        c if c.arguments is not None else replace(c, raw_arguments=_clipped(c.raw_arguments))
        for c in message.tool_calls
    )
    return replace(message, tool_calls=calls)


def _clipped(raw: str) -> str:
    """Неразобранные аргументы для модели и БД — не длиннее RAW_ARGUMENTS_CHARS (AG-11)."""
    if len(raw) <= RAW_ARGUMENTS_CHARS:
        return raw
    return f"{raw[:RAW_ARGUMENTS_CHARS]}…(обрезано, {len(raw)} символов)"


def _unparseable(call: ToolCall) -> ToolResult:
    return ToolResult(
        error=ToolError(
            code="validation_error",
            message=f"{call.name}: аргументы должны быть JSON-объектом, получено: "
            f"{_clipped(call.raw_arguments)!r}",
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
