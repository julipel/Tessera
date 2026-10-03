"""Адаптер LLMClient для Anthropic Messages API (стрим + вызов инструментов).

Стрим разбирается по сырым событиям: `content_block_start` с блоком `tool_use` несёт id и имя,
аргументы приходят фрагментами `input_json_delta` по индексу блока. Блоки размышлений и прочие
служебные блоки пропускаются. Ошибки SDK переводятся в `LLMError` с признаком `retryable`.

Параметры сэмплинга (ADR-0009): не передаёт ни один. Актуальные модели Anthropic их не
принимают, и SDK убрал их из сигнатуры `messages.create`; заданная `LLMRequest.temperature`
отбрасывается с debug-логом `llm.param_dropped`.
"""

from collections.abc import AsyncIterator
from dataclasses import dataclass

from anthropic import (
    APIError,
    APIStatusError,
    AsyncAnthropic,
    omit,
)
from anthropic.types import (
    ContentBlockParam,
    MessageParam,
    RawMessageStreamEvent,
    ToolParam,
    ToolResultBlockParam,
)

from app.modules.agent.domain.llm import (
    AssistantMessage,
    LLMChunk,
    LLMError,
    LLMRequest,
    LLMResponse,
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
from app.modules.agent.infrastructure.sampling import log_dropped_param

# 408 Request Timeout, 409 Conflict, 429 Rate Limit — временные, как и 5xx (включая 529 Overloaded).
_RETRYABLE_STATUSES = frozenset({408, 409, 429})
# Ошибка внутри стрима приходит при HTTP 200 — классифицируем её по типу из тела события.
_RETRYABLE_ERROR_TYPES = frozenset(
    {"rate_limit_error", "api_error", "overloaded_error", "timeout_error"}
)

_STOP_REASONS = {
    "end_turn": StopReason.END_TURN,
    "stop_sequence": StopReason.END_TURN,
    "tool_use": StopReason.TOOL_CALLS,
    "max_tokens": StopReason.MAX_TOKENS,
    "model_context_window_exceeded": StopReason.MAX_TOKENS,
}

# В Anthropic max_tokens обязателен; используется, если в запросе лимит не задан.
DEFAULT_MAX_TOKENS = 4096


def create_anthropic_llm(
    api_key: str,
    *,
    base_url: str | None = None,
    timeout_s: float = 60.0,
    max_retries: int = 2,
    default_max_tokens: int = DEFAULT_MAX_TOKENS,
) -> "AnthropicLLM":
    client = AsyncAnthropic(
        api_key=api_key, base_url=base_url, timeout=timeout_s, max_retries=max_retries
    )
    return AnthropicLLM(client, default_max_tokens=default_max_tokens)


class AnthropicLLM:
    def __init__(
        self, client: AsyncAnthropic, *, default_max_tokens: int = DEFAULT_MAX_TOKENS
    ) -> None:
        self._client = client
        self._default_max_tokens = default_max_tokens

    async def stream(self, request: LLMRequest) -> AsyncIterator[LLMChunk]:
        if request.temperature is not None:
            log_dropped_param("anthropic", request.model, "temperature")
        accumulator = _StreamAccumulator()
        try:
            stream = await self._client.messages.create(
                model=request.model,
                system=request.system or omit,
                messages=to_anthropic_messages(request),
                tools=[to_anthropic_tool(tool) for tool in request.tools] or omit,
                max_tokens=request.max_output_tokens or self._default_max_tokens,
                stream=True,
            )
            async with stream:
                async for event in stream:
                    for item in accumulator.feed(event):
                        yield item
        except APIError as error:
            raise to_llm_error(error) from error
        yield ResponseCompleted(accumulator.response())


def to_anthropic_messages(request: LLMRequest) -> list[MessageParam]:
    """Результаты инструментов — блоки user-сообщения; идущие подряд склеиваются в одно,
    потому что API ждёт все tool_result шага в одном сообщении сразу после tool_use."""
    messages: list[MessageParam] = []
    pending_results: list[ToolResultBlockParam] = []

    def flush_results() -> None:
        if pending_results:
            messages.append({"role": "user", "content": list(pending_results)})
            pending_results.clear()

    for message in request.messages:
        match message:
            case ToolResultMessage(tool_call_id=call_id, content=content, is_error=is_error):
                result: ToolResultBlockParam = {
                    "type": "tool_result",
                    "tool_use_id": call_id,
                    "content": content,
                }
                if is_error:
                    result["is_error"] = True
                pending_results.append(result)
                continue
            case UserMessage(text=text):
                flush_results()
                messages.append({"role": "user", "content": text})
            case AssistantMessage(text=text, tool_calls=()):
                flush_results()
                messages.append({"role": "assistant", "content": text})
            case AssistantMessage(text=text, tool_calls=calls):
                flush_results()
                # Пустой текстовый блок API отклоняет — добавляем только непустой.
                blocks: list[ContentBlockParam] = [{"type": "text", "text": text}] if text else []
                blocks.extend(
                    {
                        "type": "tool_use",
                        "id": call.id,
                        "name": call.name,
                        # Неразобранные аргументы цикл уже отклонил; input обязан быть объектом.
                        "input": call.arguments if call.arguments is not None else {},
                    }
                    for call in calls
                )
                messages.append({"role": "assistant", "content": blocks})
    flush_results()
    return messages


def to_anthropic_tool(tool: ToolSchema) -> ToolParam:
    return {
        "name": tool.name,
        "description": tool.description,
        "input_schema": tool.parameters,
    }


def to_llm_error(error: APIError) -> LLMError:
    """Таймаут/соединение, 408/409/429 и 5xx — retryable; прочие 4xx — нет. Событие `error`
    внутри стрима (HTTP 200) — по типу ошибки: перегрузка/лимит/сбой API — retryable."""
    if isinstance(error, APIStatusError):
        status = error.status_code
        if status < 400:
            retryable = error.type is None or error.type in _RETRYABLE_ERROR_TYPES
            return LLMError(f"Anthropic: ошибка стрима: {error.message}", retryable=retryable)
        retryable = status in _RETRYABLE_STATUSES or status >= 500
        return LLMError(f"Anthropic: HTTP {status}: {error.message}", retryable=retryable)
    # Соединение/таймаут (APIConnectionError) и обрыв ответа — временные сбои.
    return LLMError(f"Anthropic: {type(error).__name__}: {error.message}", retryable=True)


@dataclass(slots=True)
class _PendingCall:
    id: str
    name: str
    arguments: str = ""


class _StreamAccumulator:
    def __init__(self) -> None:
        self._text: list[str] = []
        self._calls: dict[int, _PendingCall] = {}
        self._stop_reason: str | None = None
        self._input_tokens = 0
        self._output_tokens = 0

    def feed(self, event: RawMessageStreamEvent) -> list[LLMChunk]:
        out: list[LLMChunk] = []
        match event.type:
            case "message_start":
                usage = event.message.usage
                self._input_tokens = (
                    usage.input_tokens
                    + (usage.cache_creation_input_tokens or 0)
                    + (usage.cache_read_input_tokens or 0)
                )
                self._output_tokens = usage.output_tokens
            case "content_block_start":
                block = event.content_block
                if block.type == "tool_use":
                    self._calls[event.index] = _PendingCall(id=block.id, name=block.name)
                    out.append(ToolCallStarted(id=block.id, name=block.name))
                elif block.type == "text" and block.text:
                    out.append(self._add_text(block.text))
            case "content_block_delta":
                delta = event.delta
                if delta.type == "text_delta" and delta.text:
                    out.append(self._add_text(delta.text))
                elif delta.type == "input_json_delta" and event.index in self._calls:
                    self._calls[event.index].arguments += delta.partial_json
            case "message_delta":
                if event.delta.stop_reason is not None:
                    self._stop_reason = event.delta.stop_reason
                # output_tokens в message_delta — накопительный итог, а не приращение.
                self._output_tokens = event.usage.output_tokens
            case _:
                pass
        return out

    def _add_text(self, text: str) -> TextDelta:
        self._text.append(text)
        return TextDelta(text)

    def response(self) -> LLMResponse:
        calls = tuple(
            ToolCall(
                id=call.id,
                name=call.name,
                arguments=parse_arguments(call.arguments),
                raw_arguments=call.arguments,
            )
            for _, call in sorted(self._calls.items())
        )
        stop_reason = _STOP_REASONS.get(self._stop_reason or "", StopReason.END_TURN)
        if calls and stop_reason is StopReason.END_TURN:
            stop_reason = StopReason.TOOL_CALLS
        return LLMResponse(
            text="".join(self._text),
            tool_calls=calls,
            stop_reason=stop_reason,
            usage=Usage(input_tokens=self._input_tokens, output_tokens=self._output_tokens),
        )
