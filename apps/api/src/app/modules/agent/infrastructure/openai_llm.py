"""Адаптер LLMClient для OpenAI Chat Completions (стрим + вызов инструментов).

Подходит и для OpenAI-совместимых API через `base_url`. Чанки стрима собираются вручную:
фрагменты tool_calls приходят по `index` — первый несёт id и имя, следующие дописывают
аргументы. Совместимые API бывают неаккуратны: имя повторяют в каждом фрагменте (берётся
первое) или не присылают id (подставляется `call_<index>`, анонс — перед ResponseCompleted;
REVIEW.md: AG-14). Ошибки SDK переводятся в `LLMError` с признаком `retryable`. Стрим без
`finish_reason` (соединение закрыто раньше) — retryable `LLMError`, а не оборванный ответ.

Параметры сэмплинга (ADR-0009): передаёт `temperature`, если она задана. Поддержка зависит от
модели (reasoning-модели принимают только значение по умолчанию), поэтому таблицы моделей нет:
на 400 с `param: "temperature"` адаптер один раз повторяет запрос без неё и запоминает модель
до конца жизни процесса; дальше параметр для неё отбрасывается с debug-логом
`llm.param_dropped`. Ошибка приходит до начала стрима, повтор не дублирует ответ.

Лимит вывода (ADR-0042) уходит как `max_completion_tokens`. Совместимый API, отклонивший его
(400 с этим параметром), так же один раз получает повтор со старым `max_tokens`; модель
запоминается, лог `llm.legacy_max_tokens`.
"""

from collections.abc import AsyncIterator
from dataclasses import dataclass

import structlog
from openai import (
    APIConnectionError,
    APIError,
    APIStatusError,
    AsyncOpenAI,
    AsyncStream,
    BadRequestError,
    Omit,
    omit,
)
from openai.types.chat import (
    ChatCompletionChunk,
    ChatCompletionFunctionToolParam,
    ChatCompletionMessageParam,
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

logger = structlog.get_logger(__name__)

# 408 Request Timeout, 409 Conflict, 429 Rate Limit — временные, как и 5xx (их же ретраит SDK).
_RETRYABLE_STATUSES = frozenset({408, 409, 429})

# Коды OpenAI для параметра, который модель не принимает (или принимает только по умолчанию).
_UNSUPPORTED_CODES = frozenset({"unsupported_parameter", "unsupported_value"})

_STOP_REASONS = {
    "stop": StopReason.END_TURN,
    "tool_calls": StopReason.TOOL_CALLS,
    "length": StopReason.MAX_TOKENS,
}


def create_openai_llm(
    api_key: str,
    *,
    base_url: str | None = None,
    timeout_s: float = 60.0,
    max_retries: int = 2,
) -> "OpenAILLM":
    client = AsyncOpenAI(
        api_key=api_key, base_url=base_url, timeout=timeout_s, max_retries=max_retries
    )
    return OpenAILLM(client)


class OpenAILLM:
    def __init__(self, client: AsyncOpenAI) -> None:
        self._client = client
        self._no_temperature: set[str] = set()  # модели, отклонившие temperature
        self._legacy_max_tokens: set[str] = set()  # модели, отклонившие max_completion_tokens

    async def stream(self, request: LLMRequest) -> AsyncIterator[LLMChunk]:
        accumulator = _StreamAccumulator()
        try:
            stream = await self._open(request)
            async with stream:
                async for chunk in stream:
                    for item in accumulator.feed(chunk):
                        yield item
        except APIError as error:
            raise to_llm_error(error, accumulator.usage) from error
        # Сначала проверка конца стрима: оборванный стрим — retryable LLMError до анонсов,
        # иначе FallbackLLM не повторит его (уже что-то отдано) и останется висячий tool_started.
        accumulator.check_finished()
        for item in accumulator.unannounced():
            yield item
        yield ResponseCompleted(accumulator.response())

    async def _open(self, request: LLMRequest) -> AsyncStream[ChatCompletionChunk]:
        temperature = request.temperature
        if temperature is not None and request.model in self._no_temperature:
            log_dropped_param("openai", request.model, "temperature")
            temperature = None
        legacy = request.model in self._legacy_max_tokens
        # Не больше одного отката на каждый параметр: temperature и max_completion_tokens.
        for _ in range(3):
            try:
                return await self._create(request, temperature, legacy_max_tokens=legacy)
            except BadRequestError as error:
                if temperature is not None and rejects_param(error, "temperature"):
                    self._no_temperature.add(request.model)
                    log_dropped_param("openai", request.model, "temperature")
                    temperature = None
                elif (
                    not legacy
                    and request.max_output_tokens is not None
                    and _rejects_max_completion_tokens(error)
                ):
                    self._legacy_max_tokens.add(request.model)
                    logger.info("llm.legacy_max_tokens", provider="openai", model=request.model)
                    legacy = True
                else:
                    raise
        raise AssertionError("unreachable")  # pragma: no cover

    async def _create(
        self, request: LLMRequest, temperature: float | None, *, legacy_max_tokens: bool
    ) -> AsyncStream[ChatCompletionChunk]:
        limit = given(request.max_output_tokens)
        return await self._client.chat.completions.create(
            model=request.model,
            messages=to_openai_messages(request),
            tools=[to_openai_tool(tool) for tool in request.tools] or omit,
            temperature=given(temperature),
            # Совместимые API, не знающие max_completion_tokens, получают старый max_tokens.
            max_completion_tokens=omit if legacy_max_tokens else limit,
            max_tokens=limit if legacy_max_tokens else omit,
            stream=True,
            stream_options={"include_usage": True},
        )


def _rejects_max_completion_tokens(error: BadRequestError) -> bool:
    """400 совместимого API на max_completion_tokens: формат ошибок у них разный, поэтому
    достаточно имени параметра в `param` или в тексте ошибки."""
    return error.param == "max_completion_tokens" or "max_completion_tokens" in error.message


def rejects_param(error: BadRequestError, param: str) -> bool:
    """400 OpenAI: модель не принимает параметр (общее с адаптером Responses API)."""
    return error.param == param and error.code in _UNSUPPORTED_CODES


def to_openai_messages(request: LLMRequest) -> list[ChatCompletionMessageParam]:
    messages: list[ChatCompletionMessageParam] = []
    if request.system:
        messages.append({"role": "system", "content": request.system})
    for message in request.messages:
        match message:
            case UserMessage(text=text):
                messages.append({"role": "user", "content": text})
            case AssistantMessage(text=text, tool_calls=()):
                messages.append({"role": "assistant", "content": text})
            case AssistantMessage(text=text, tool_calls=calls):
                messages.append(
                    {
                        "role": "assistant",
                        "content": text or None,
                        "tool_calls": [
                            {
                                "id": call.id,
                                "type": "function",
                                "function": {"name": call.name, "arguments": call.raw_arguments},
                            }
                            for call in calls
                        ],
                    }
                )
            case ToolResultMessage(tool_call_id=call_id, content=content):
                # Флага ошибки для tool-сообщения в OpenAI нет: текст ошибки уже в content.
                messages.append({"role": "tool", "tool_call_id": call_id, "content": content})
    return messages


def to_openai_tool(tool: ToolSchema) -> ChatCompletionFunctionToolParam:
    return {
        "type": "function",
        "function": {
            "name": tool.name,
            "description": tool.description,
            "parameters": tool.parameters,
        },
    }


def to_llm_error(error: APIError, usage: Usage | None = None) -> LLMError:
    """Таймаут/соединение/обрыв стрима, 408/409/429 и 5xx — retryable; прочие 4xx — нет.
    `usage` — токены, известные до ошибки."""
    if isinstance(error, APIStatusError):
        status = error.status_code
        retryable = status in _RETRYABLE_STATUSES or status >= 500
        return LLMError(f"OpenAI: HTTP {status}: {error.message}", retryable=retryable, usage=usage)
    if isinstance(error, APIConnectionError):
        return LLMError(
            f"OpenAI: {type(error).__name__}: {error.message}", retryable=True, usage=usage
        )
    # Ошибка, пришедшая событием внутри уже открытого стрима, — сбой на стороне провайдера.
    return LLMError(f"OpenAI: ошибка стрима: {error.message}", retryable=True, usage=usage)


@dataclass(slots=True)
class _PendingCall:
    id: str = ""
    name: str = ""
    arguments: str = ""
    announced: bool = False


class _StreamAccumulator:
    def __init__(self) -> None:
        self._text: list[str] = []
        self._calls: dict[int, _PendingCall] = {}
        self._finish_reason: str | None = None
        self._usage: Usage | None = None  # приходит последним чанком (include_usage)

    @property
    def usage(self) -> Usage | None:
        return self._usage

    def feed(self, chunk: ChatCompletionChunk) -> list[LLMChunk]:
        if chunk.usage is not None:
            # prompt_tokens включает кэшированные; запись в кэш OpenAI отдельно не тарифицирует.
            # SDK ответ не валидирует: у совместимых API details может не быть.
            details = getattr(chunk.usage, "prompt_tokens_details", None)
            self._usage = Usage(
                input_tokens=chunk.usage.prompt_tokens,
                output_tokens=chunk.usage.completion_tokens,
                cache_read_tokens=getattr(details, "cached_tokens", None) or 0,
            )
        out: list[LLMChunk] = []
        for choice in chunk.choices:
            if choice.index != 0:
                continue
            delta = choice.delta
            for text in (delta.content, delta.refusal):
                if text:
                    self._text.append(text)
                    out.append(TextDelta(text))
            for fragment in delta.tool_calls or ():
                call = self._calls.setdefault(fragment.index, _PendingCall())
                if fragment.id:
                    call.id = fragment.id
                if fragment.function is not None:
                    if fragment.function.name and not call.name:
                        call.name = fragment.function.name
                    if fragment.function.arguments:
                        call.arguments += fragment.function.arguments
                if not call.announced and call.id and call.name:
                    call.announced = True
                    out.append(ToolCallStarted(id=call.id, name=call.name))
            # Некоторые совместимые API шлют finish_reason="" в промежуточных чанках.
            if choice.finish_reason:
                self._finish_reason = choice.finish_reason
        return out

    def unannounced(self) -> list[LLMChunk]:
        """Конец стрима: вызовам без id — `call_<index>`, и анонс тех, что не анонсированы."""
        out: list[LLMChunk] = []
        for index, call in sorted(self._calls.items()):
            if not call.id:
                call.id = f"call_{index}"
            if not call.announced and call.name:
                call.announced = True
                out.append(ToolCallStarted(id=call.id, name=call.name))
        return out

    def check_finished(self) -> None:
        if self._finish_reason is None:
            raise LLMError(
                "OpenAI: стрим закрыт без finish_reason", retryable=True, usage=self._usage
            )

    def response(self) -> LLMResponse:
        self.check_finished()
        assert self._finish_reason is not None
        calls = tuple(
            ToolCall(
                id=call.id,
                name=call.name,
                arguments=parse_arguments(call.arguments),
                raw_arguments=call.arguments,
            )
            for _, call in sorted(self._calls.items())
        )
        stop_reason = _STOP_REASONS.get(self._finish_reason, StopReason.END_TURN)
        if calls and stop_reason is StopReason.END_TURN:
            # Некоторые совместимые API отвечают finish_reason="stop" и при вызове инструментов.
            stop_reason = StopReason.TOOL_CALLS
        return LLMResponse(
            text="".join(self._text),
            tool_calls=calls,
            stop_reason=stop_reason,
            usage=self._usage or Usage(),
        )


def given[T](value: T | None) -> T | Omit:
    """None — не передавать параметр в запрос."""
    return omit if value is None else value
