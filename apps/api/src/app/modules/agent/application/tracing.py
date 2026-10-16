"""Трейсинг хода (P7-03, ADR-0029): вызовы модели и инструментов в трейсе хода.

`TracedLLM` оборачивает клиент одной модели — внутри `FallbackLLM` каждая попытка (повтор,
переход на резервную) становится отдельной generation. `traced_turn` ведёт трейс хода
по событиям агента: инструменты, итог хода, ошибка или отмена.
"""

import asyncio
from collections.abc import AsyncGenerator, AsyncIterator
from typing import Any

from app.modules.agent.domain.events import (
    AgentEvent,
    AnswerDelta,
    ToolFinished,
    ToolStarted,
    TurnCompleted,
)
from app.modules.agent.domain.llm import (
    AssistantMessage,
    LLMChunk,
    LLMClient,
    LLMError,
    LLMMessage,
    LLMRequest,
    ResponseCompleted,
    ToolResultMessage,
    Usage,
    UserMessage,
)
from app.modules.observability.kernel import GenerationInfo, GenerationUsage, TurnTrace


class TracedLLM:
    """`LLMClient`, который пишет каждый вызов `stream` в трейс хода как generation.
    `provider` — провайдер модели: от него зависят корзины токенов в трейсе (обязателен —
    Anthropic в корзинах OpenAI молча исказил бы стоимость)."""

    def __init__(self, inner: LLMClient, trace: TurnTrace, provider: str) -> None:
        self._inner = inner
        self._trace = trace
        self._provider = provider

    async def stream(self, request: LLMRequest) -> AsyncIterator[LLMChunk]:
        generation = self._trace.generation(
            GenerationInfo(
                name="llm",
                model=request.model,
                input={"system": request.system, "messages": _messages(request.messages)},
                parameters={
                    "temperature": request.temperature,
                    "max_output_tokens": request.max_output_tokens,
                    "tools": [t.name for t in request.tools],
                },
                provider=self._provider,
            )
        )
        finished = False
        try:
            async for chunk in self._inner.stream(request):
                generation.first_chunk()
                if isinstance(chunk, ResponseCompleted):
                    response = chunk.response
                    generation.finish(
                        {
                            "text": response.text,
                            "tool_calls": [
                                {"id": c.id, "name": c.name, "arguments": c.raw_arguments}
                                for c in response.tool_calls
                            ],
                            "stop_reason": response.stop_reason.value,
                        },
                        _usage(response.usage),
                    )
                    finished = True
                yield chunk
        except BaseException as e:  # в т.ч. отмена хода и закрытие генератора
            if not finished:
                usage = e.usage if isinstance(e, LLMError) else None
                generation.fail(_reason(e), _usage(usage) if usage is not None else None)
                finished = True
            raise
        finally:
            if not finished:  # стрим кончился без ResponseCompleted
                generation.fail("стрим без финального ответа")


async def traced_turn(
    events: AsyncIterator[AgentEvent], trace: TurnTrace
) -> AsyncGenerator[AgentEvent]:
    """События хода без изменений; по ним — инструменты и итог в трейсе."""
    answer: list[str] = []
    output: dict[str, Any] = {}
    try:
        async for event in events:
            match event:
                case AnswerDelta(text=text):
                    answer.append(text)
                case ToolStarted(tool_call_id=call_id, name=name):
                    trace.tool_started(call_id, name)
                case ToolFinished() as finished:
                    trace.tool_finished(
                        finished.tool_call_id,
                        finished.name,
                        finished.arguments,
                        finished.content,
                        f"{finished.error_code}: {finished.error_message or ''}"
                        if finished.error_code
                        else None,
                        finished.duration_ms,
                    )
                case TurnCompleted(finish=finish, usage=usage, steps=steps):
                    output = {
                        "finish": finish.value,
                        "steps": steps,
                        "input_tokens": usage.input_tokens,
                        "output_tokens": usage.output_tokens,
                    }
            yield event
    except BaseException as e:
        trace.finish({"answer": "".join(answer), **output}, _reason(e))
        raise
    trace.finish({"answer": "".join(answer), **output})


def _usage(usage: Usage) -> GenerationUsage:
    return GenerationUsage(
        input_tokens=usage.input_tokens,
        output_tokens=usage.output_tokens,
        cache_read_tokens=usage.cache_read_tokens,
        cache_write_tokens=usage.cache_write_tokens,
    )


def _reason(error: BaseException) -> str:
    if isinstance(error, (asyncio.CancelledError, GeneratorExit)):
        return "прерван"
    return f"{type(error).__name__}: {error}"


def _messages(messages: tuple[LLMMessage, ...]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for m in messages:
        match m:
            case UserMessage(text=text):
                result.append({"role": "user", "content": text})
            case AssistantMessage(text=text, tool_calls=calls):
                item: dict[str, Any] = {"role": "assistant", "content": text}
                if calls:
                    item["tool_calls"] = [
                        {"id": c.id, "name": c.name, "arguments": c.raw_arguments} for c in calls
                    ]
                result.append(item)
            case ToolResultMessage(tool_call_id=call_id, content=content, is_error=is_error):
                result.append(
                    {
                        "role": "tool",
                        "tool_call_id": call_id,
                        "content": content,
                        "is_error": is_error,
                    }
                )
    return result
