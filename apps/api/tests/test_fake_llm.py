"""FakeLLM: сценарный LLMClient для тестов агентного цикла (P2-01)."""

import asyncio

import pytest

from app.modules.agent.public import (
    FakeLLM,
    FakeLLMExhaustedError,
    FakeReply,
    LLMChunk,
    LLMClient,
    LLMError,
    LLMRequest,
    ResponseCompleted,
    StopReason,
    TextDelta,
    ToolCall,
    ToolCallStarted,
    ToolResultMessage,
    Usage,
    UserMessage,
)


def request(text: str = "Привет") -> LLMRequest:
    return LLMRequest(model="fake", system="Ты — консультант.", messages=(UserMessage(text),))


async def collect(llm: LLMClient, req: LLMRequest | None = None) -> list[LLMChunk]:
    return [chunk async for chunk in llm.stream(req or request())]


def completed(chunks: list[LLMChunk]) -> ResponseCompleted:
    last = chunks[-1]
    assert isinstance(last, ResponseCompleted)
    return last


SEARCH = ToolCall(
    id="call_1",
    name="search_catalog",
    arguments={"query": "крем"},
    raw_arguments='{"query":"крем"}',
)


async def test_text_is_streamed_by_words_and_completed() -> None:
    chunks = await collect(FakeLLM([FakeReply(text="Чем могу помочь?")]))

    deltas = [c.text for c in chunks if isinstance(c, TextDelta)]
    assert deltas == ["Чем ", "могу ", "помочь?"]
    response = completed(chunks).response
    assert response.text == "Чем могу помочь?"
    assert response.tool_calls == ()
    assert response.stop_reason is StopReason.END_TURN
    assert sum(isinstance(c, ResponseCompleted) for c in chunks) == 1


async def test_explicit_chunks_override_text() -> None:
    chunks = await collect(FakeLLM([FakeReply(text="игнор", chunks=("При", "вет"))]))

    assert [c.text for c in chunks if isinstance(c, TextDelta)] == ["При", "вет"]
    assert completed(chunks).response.text == "Привет"


async def test_tool_calls_are_announced_then_returned_in_response() -> None:
    usage = Usage(input_tokens=120, output_tokens=15)
    chunks = await collect(FakeLLM([FakeReply(text="Ищу. ", tool_calls=(SEARCH,), usage=usage)]))

    assert chunks[:2] == [TextDelta("Ищу. "), ToolCallStarted(id="call_1", name="search_catalog")]
    response = completed(chunks).response
    assert response.tool_calls == (SEARCH,)
    assert response.stop_reason is StopReason.TOOL_CALLS
    assert response.usage == usage
    assert response.as_message().tool_calls == (SEARCH,)


async def test_provider_items_are_returned_in_response_and_message() -> None:
    items = ({"type": "reasoning", "encrypted_content": "gAAA…"},)
    chunks = await collect(FakeLLM([FakeReply(tool_calls=(SEARCH,), provider_items=items)]))

    response = completed(chunks).response
    assert response.provider_items == items
    assert response.as_message().provider_items == items


async def test_invalid_arguments_are_passed_through_raw() -> None:
    broken = ToolCall(id="call_2", name="search_catalog", arguments=None, raw_arguments="{query:")
    response = completed(await collect(FakeLLM([FakeReply(tool_calls=(broken,))]))).response

    assert response.tool_calls[0].arguments is None
    assert response.tool_calls[0].raw_arguments == "{query:"


async def test_steps_are_consumed_in_order_and_requests_recorded() -> None:
    llm = FakeLLM([FakeReply(tool_calls=(SEARCH,)), FakeReply(text="Нашёл два крема.")])
    first = request("Подбери крем")
    second = LLMRequest(
        model="fake",
        system=first.system,
        messages=(
            *first.messages,
            completed(await collect(llm, first)).response.as_message(),
            ToolResultMessage(tool_call_id="call_1", content='{"items": 2}'),
        ),
    )

    response = completed(await collect(llm, second)).response

    assert response.text == "Нашёл два крема."
    assert llm.requests == [first, second]
    assert llm.remaining == 0


async def test_exhausted_scenario_fails_loudly() -> None:
    llm = FakeLLM([FakeReply(text="Ок")])
    await collect(llm)

    with pytest.raises(FakeLLMExhaustedError, match="№2"):
        await collect(llm)


async def test_error_step_raises_before_first_chunk() -> None:
    llm = FakeLLM([LLMError("429", retryable=True)])
    received: list[LLMChunk] = []

    with pytest.raises(LLMError) as exc:
        async for chunk in llm.stream(request()):
            received.append(chunk)

    assert exc.value.retryable is True
    assert received == []


async def test_stream_can_break_mid_way() -> None:
    error = LLMError("соединение оборвано", retryable=True)
    llm = FakeLLM([FakeReply(text="раз два три", fail_after_chunks=2, error=error)])
    received: list[LLMChunk] = []

    with pytest.raises(LLMError) as exc:
        async for chunk in llm.stream(request()):
            received.append(chunk)

    assert exc.value is error
    assert received == [TextDelta("раз "), TextDelta("два ")]


async def test_delay_allows_timeout_and_cancellation() -> None:
    llm = FakeLLM([FakeReply(text="долго", delay_s=10), FakeReply(text="долго", delay_s=10)])

    with pytest.raises(TimeoutError):
        async with asyncio.timeout(0.05):
            await collect(llm)

    task = asyncio.create_task(collect(llm))
    await asyncio.sleep(0.01)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert len(llm.requests) == 2


def test_fake_llm_satisfies_port() -> None:
    client: LLMClient = FakeLLM()  # mypy проверяет структурное соответствие Protocol
    assert client is not None
