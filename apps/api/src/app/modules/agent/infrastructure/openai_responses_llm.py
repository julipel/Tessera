"""Адаптер LLMClient для OpenAI Responses API (ADR-0010): стрим + вызов инструментов.

Без состояния на стороне OpenAI: `store: false`, полный контекст в `input` на каждом шаге,
`previous_response_id` не используется. Reasoning items приходят с
`reasoning.encrypted_content` и уходят в `LLMResponse.provider_items`; в следующем шаге хода
адаптер возвращает их во `input` перед текстом и вызовами инструментов того же ответа —
в порядке, в котором модель их выдала.

Стрим: `response.output_text.delta`/`response.refusal.delta` → TextDelta;
`response.output_item.added` с function_call → ранний ToolCallStarted; готовые элементы
(аргументы вызова, reasoning целиком) — из `response.output_item.done`; usage — из
`response.completed`/`response.incomplete` (лимит токенов → MAX_TOKENS); `response.failed` и
событие `error` → LLMError.

Передаваемые параметры (ADR-0009): `instructions`, `input`, `tools`, `max_output_tokens`,
`temperature`, если задана. Temperature отбрасывается адаптивно, как в OpenAILLM: на 400 с
`param: "temperature"` — один повтор без неё, модель запоминается до конца жизни процесса,
дальше параметр отбрасывается с debug-логом `llm.param_dropped`.
"""

from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import cast

import structlog
from openai import APIError, AsyncOpenAI, AsyncStream, BadRequestError, omit
from openai.types.responses import (
    FunctionToolParam,
    ResponseCompletedEvent,
    ResponseErrorEvent,
    ResponseFailedEvent,
    ResponseFunctionToolCall,
    ResponseIncompleteEvent,
    ResponseInputItemParam,
    ResponseOutputItemAddedEvent,
    ResponseOutputItemDoneEvent,
    ResponseReasoningItem,
    ResponseRefusalDeltaEvent,
    ResponseStreamEvent,
    ResponseTextDeltaEvent,
    ResponseUsage,
)

from app.modules.agent.domain.llm import (
    AssistantMessage,
    LLMChunk,
    LLMError,
    LLMRequest,
    LLMResponse,
    ProviderItem,
    ResponseCompleted,
    StopReason,
    TextDelta,
    ToolCall,
    ToolCallStarted,
    ToolResultMessage,
    ToolSchema,
    Usage,
    UserMessage,
)
from app.modules.agent.infrastructure.llm_arguments import parse_arguments
from app.modules.agent.infrastructure.openai_llm import given, rejects_param, to_llm_error
from app.modules.agent.infrastructure.sampling import log_dropped_param

logger = structlog.get_logger(__name__)


def create_openai_responses_llm(
    api_key: str,
    *,
    base_url: str | None = None,
    timeout_s: float = 60.0,
    max_retries: int = 2,
) -> "OpenAIResponsesLLM":
    client = AsyncOpenAI(
        api_key=api_key, base_url=base_url, timeout=timeout_s, max_retries=max_retries
    )
    return OpenAIResponsesLLM(client)


class OpenAIResponsesLLM:
    def __init__(self, client: AsyncOpenAI) -> None:
        self._client = client
        self._no_temperature: set[str] = set()  # модели, отклонившие temperature

    async def stream(self, request: LLMRequest) -> AsyncIterator[LLMChunk]:
        accumulator = _StreamAccumulator()
        try:
            stream = await self._open(request)
            async with stream:
                async for event in stream:
                    for item in accumulator.feed(event):
                        yield item
        except APIError as error:
            raise to_llm_error(error) from error
        yield ResponseCompleted(accumulator.response())

    async def _open(self, request: LLMRequest) -> AsyncStream[ResponseStreamEvent]:
        temperature = request.temperature
        if temperature is not None and request.model in self._no_temperature:
            log_dropped_param("openai", request.model, "temperature")
            temperature = None
        try:
            return await self._create(request, temperature)
        except BadRequestError as error:
            if temperature is None or not rejects_param(error, "temperature"):
                raise
            self._no_temperature.add(request.model)
            log_dropped_param("openai", request.model, "temperature")
            return await self._create(request, None)

    async def _create(
        self, request: LLMRequest, temperature: float | None
    ) -> AsyncStream[ResponseStreamEvent]:
        return await self._client.responses.create(
            model=request.model,
            instructions=given(request.system or None),
            input=to_responses_input(request),
            tools=[to_responses_tool(tool) for tool in request.tools] or omit,
            temperature=given(temperature),
            max_output_tokens=given(request.max_output_tokens),
            store=False,
            include=["reasoning.encrypted_content"],
            stream=True,
        )


def to_responses_input(request: LLMRequest) -> list[ResponseInputItemParam]:
    items: list[ResponseInputItemParam] = []
    for message in request.messages:
        match message:
            case UserMessage(text=text):
                items.append({"role": "user", "content": text})
            case AssistantMessage(text=text, tool_calls=calls, provider_items=provider_items):
                # Элементы провайдера — как пришли: модель проверяет encrypted_content.
                items.extend(cast(ResponseInputItemParam, item) for item in provider_items)
                if text:
                    items.append({"role": "assistant", "content": text})
                items.extend(
                    {
                        "type": "function_call",
                        "call_id": call.id,
                        "name": call.name,
                        "arguments": call.raw_arguments,
                    }
                    for call in calls
                )
            case ToolResultMessage(tool_call_id=call_id, content=content):
                # Флага ошибки у function_call_output нет: текст ошибки уже в content.
                items.append(
                    {"type": "function_call_output", "call_id": call_id, "output": content}
                )
    return items


def to_responses_tool(tool: ToolSchema) -> FunctionToolParam:
    # strict выключен: он требует особой формы JSON Schema (все поля required и т.п.),
    # а схемы инструментов тенантов произвольные; аргументы и так валидирует Tool Registry.
    return {
        "type": "function",
        "name": tool.name,
        "description": tool.description,
        "parameters": tool.parameters,
        "strict": False,
    }


def to_provider_item(item: ResponseReasoningItem) -> ProviderItem:
    """Reasoning item для возврата во `input`: без `status` (поле только вывода), как в
    примере OpenAI для stateless-режима; `encrypted_content` не меняется."""
    return item.model_dump(mode="json", exclude={"status"}, exclude_none=True)


@dataclass(slots=True)
class _PendingCall:
    call_id: str
    name: str
    arguments: str | None = None


class _StreamAccumulator:
    def __init__(self) -> None:
        self._text: list[str] = []
        self._calls: dict[int, _PendingCall] = {}  # по output_index
        self._provider_items: list[tuple[int, ProviderItem]] = []
        self._usage = Usage()
        self._stop_reason = StopReason.END_TURN

    def feed(self, event: ResponseStreamEvent) -> list[LLMChunk]:
        match event:
            case ResponseTextDeltaEvent(delta=text) | ResponseRefusalDeltaEvent(delta=text):
                if text:
                    self._text.append(text)
                    return [TextDelta(text)]
            case ResponseOutputItemAddedEvent(
                output_index=index, item=ResponseFunctionToolCall() as call
            ):
                self._calls[index] = _PendingCall(call_id=call.call_id, name=call.name)
                return [ToolCallStarted(id=call.call_id, name=call.name)]
            case ResponseOutputItemDoneEvent(
                output_index=index, item=ResponseFunctionToolCall() as call
            ):
                pending = self._calls.setdefault(
                    index, _PendingCall(call_id=call.call_id, name=call.name)
                )
                pending.arguments = call.arguments
            case ResponseOutputItemDoneEvent(
                output_index=index, item=ResponseReasoningItem() as reasoning
            ):
                self._provider_items.append((index, to_provider_item(reasoning)))
            case ResponseCompletedEvent(response=response):
                self._set_usage(response.usage)
            case ResponseIncompleteEvent(response=response):
                self._set_usage(response.usage)
                details = response.incomplete_details
                reason = details.reason if details else None
                if reason == "max_output_tokens":
                    self._stop_reason = StopReason.MAX_TOKENS
                else:
                    # content_filter и прочее: текст уже ушёл в стрим — завершаем шаг как есть.
                    logger.warning("llm.response_incomplete", provider="openai", reason=reason)
            case ResponseFailedEvent(response=response):
                error = response.error
                message = f"{error.code}: {error.message}" if error else "unknown"
                raise LLMError(f"OpenAI: ответ не получен: {message}", retryable=True)
            case ResponseErrorEvent(code=code, message=message):
                raise LLMError(f"OpenAI: ошибка стрима: {code}: {message}", retryable=True)
        return []

    def _set_usage(self, usage: ResponseUsage | None) -> None:
        if usage is not None:
            self._usage = Usage(input_tokens=usage.input_tokens, output_tokens=usage.output_tokens)

    def response(self) -> LLMResponse:
        calls = tuple(
            ToolCall(
                id=call.call_id,
                name=call.name,
                arguments=parse_arguments(call.arguments or ""),
                raw_arguments=call.arguments or "",
            )
            for _, call in sorted(self._calls.items())
        )
        stop_reason = self._stop_reason
        if calls and stop_reason is StopReason.END_TURN:
            stop_reason = StopReason.TOOL_CALLS
        return LLMResponse(
            text="".join(self._text),
            tool_calls=calls,
            stop_reason=stop_reason,
            usage=self._usage,
            provider_items=tuple(
                item for _, item in sorted(self._provider_items, key=lambda pair: pair[0])
            ),
        )
