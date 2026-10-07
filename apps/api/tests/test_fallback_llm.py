"""FallbackLLM (P7-01, ADR-0027): повтор шага основной модели, переход на резервную, проброс
ошибок, которые повторять нельзя."""

import pytest

from app.modules.agent.public import (
    AssistantMessage,
    FakeLLM,
    FakeReply,
    FallbackLLM,
    FallbackModel,
    LLMChunk,
    LLMClient,
    LLMError,
    LLMRequest,
    TextDelta,
    UserMessage,
)

REQUEST = LLMRequest(
    model="main-model",
    system="Ты — консультант.",
    messages=(
        UserMessage("Подбери крем"),
        AssistantMessage("", provider_items=({"type": "reasoning", "encrypted_content": "x"},)),
        UserMessage("Сухая кожа"),
    ),
    temperature=0.3,
)


def unavailable() -> LLMError:
    return LLMError("HTTP 503", retryable=True)


class Sleeps:
    def __init__(self) -> None:
        self.delays: list[float] = []

    async def __call__(self, delay: float) -> None:
        self.delays.append(delay)


def fallback_llm(
    primary: LLMClient, backup: LLMClient | None = None, sleeps: Sleeps | None = None
) -> FallbackLLM:
    fallback = (
        FallbackModel(client=lambda: backup, model="backup-model", temperature=None)
        if backup is not None
        else None
    )
    return FallbackLLM(primary, fallback, sleep=sleeps or Sleeps())


async def text(llm: LLMClient, request: LLMRequest = REQUEST) -> str:
    chunks: list[LLMChunk] = [c async for c in llm.stream(request)]
    return "".join(c.text for c in chunks if isinstance(c, TextDelta))


async def test_retryable_error_before_stream_is_retried_with_pause() -> None:
    primary = FakeLLM([unavailable(), FakeReply(text="Крем с керамидами.")])
    sleeps = Sleeps()

    assert await text(fallback_llm(primary, sleeps=sleeps)) == "Крем с керамидами."
    assert primary.requests == [REQUEST, REQUEST]
    assert sleeps.delays == [1.0]


async def test_after_primary_attempts_step_goes_to_fallback_model() -> None:
    primary = FakeLLM([unavailable(), unavailable()])
    backup = FakeLLM([FakeReply(text="Ответ резервной.")])

    assert await text(fallback_llm(primary, backup)) == "Ответ резервной."
    [request] = backup.requests
    assert (request.model, request.temperature) == ("backup-model", None)
    # Reasoning основной модели резервная не примет (ADR-0010).
    assert request.messages[1] == AssistantMessage("")
    assert request.messages[::2] == REQUEST.messages[::2]


async def test_fallback_stays_until_end_of_turn() -> None:
    primary = FakeLLM([unavailable(), unavailable()])
    backup = FakeLLM([FakeReply(text="Шаг 1."), FakeReply(text="Шаг 2.")])
    llm = fallback_llm(primary, backup)

    await text(llm)
    assert await text(llm) == "Шаг 2."
    assert len(primary.requests) == 2


async def test_error_after_first_chunk_is_not_retried() -> None:
    # Повтор задублировал бы уже отданный клиенту текст.
    primary = FakeLLM([FakeReply(text="Начало ответа", fail_after_chunks=1)])
    backup = FakeLLM()
    received: list[str] = []

    with pytest.raises(LLMError):
        async for chunk in fallback_llm(primary, backup).stream(REQUEST):
            if isinstance(chunk, TextDelta):
                received.append(chunk.text)
    assert received == ["Начало "]
    assert len(primary.requests) == 1 and backup.requests == []


async def test_non_retryable_error_is_raised_at_once() -> None:
    primary = FakeLLM([LLMError("HTTP 400: context too long", retryable=False)])
    backup = FakeLLM()

    with pytest.raises(LLMError, match="context too long"):
        await text(fallback_llm(primary, backup))
    assert backup.requests == []


async def test_without_fallback_last_primary_error_is_raised() -> None:
    last = LLMError("HTTP 429", retryable=True)
    primary = FakeLLM([unavailable(), last])

    with pytest.raises(LLMError) as raised:
        await text(fallback_llm(primary))
    assert raised.value is last


async def test_unconfigured_fallback_raises_primary_error() -> None:
    def no_key() -> LLMClient:
        raise LLMError("провайдер 'anthropic' не настроен: нет API-ключа", retryable=False)

    last = unavailable()
    llm = FallbackLLM(
        FakeLLM([unavailable(), last]),
        FallbackModel(client=no_key, model="backup-model"),
        sleep=Sleeps(),
    )

    with pytest.raises(LLMError) as raised:
        await text(llm)
    assert raised.value is last
