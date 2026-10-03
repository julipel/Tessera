"""Адаптер OpenAI (P2-06) без сети: настоящий AsyncOpenAI поверх MockTransport с SSE-ответами."""

import json
from collections.abc import Callable
from typing import Any

import httpx2
import pytest
from openai import AsyncOpenAI

from app.modules.agent.public import (
    AssistantMessage,
    LLMChunk,
    LLMError,
    LLMRequest,
    OpenAILLM,
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

type Handler = Callable[[httpx2.Request], httpx2.Response]


def sse(*chunks: dict[str, Any], done: bool = True) -> str:
    lines = [f"data: {json.dumps(chunk)}\n\n" for chunk in chunks]
    if done:
        lines.append("data: [DONE]\n\n")
    return "".join(lines)


def chunk(
    delta: dict[str, Any] | None = None,
    *,
    finish: str | None = None,
    usage: dict[str, int] | None = None,
) -> dict[str, Any]:
    choices = [] if delta is None else [{"index": 0, "delta": delta, "finish_reason": finish}]
    return {
        "id": "chatcmpl-1",
        "object": "chat.completion.chunk",
        "created": 0,
        "model": "gpt-test",
        "choices": choices,
        "usage": usage,
    }


def tool_fragment(
    index: int, *, id: str | None = None, name: str | None = None, arguments: str | None = None
) -> dict[str, Any]:
    fragment: dict[str, Any] = {"index": index, "function": {}}
    if id is not None:
        fragment |= {"id": id, "type": "function"}
    if name is not None:
        fragment["function"]["name"] = name
    if arguments is not None:
        fragment["function"]["arguments"] = arguments
    return {"tool_calls": [fragment]}


class Server:
    """Отвечает заданным SSE-телом (или статусом) и запоминает тела запросов."""

    def __init__(self, body: str = "", *, status: int = 200) -> None:
        self.body = body
        self.status = status
        self.requests: list[dict[str, Any]] = []

    def __call__(self, request: httpx2.Request) -> httpx2.Response:
        self.requests.append(json.loads(request.content))
        if self.status != 200:
            return httpx2.Response(self.status, json={"error": {"message": "boom"}})
        return httpx2.Response(200, text=self.body, headers={"content-type": "text/event-stream"})


def llm_for(handler: Handler) -> OpenAILLM:
    client = AsyncOpenAI(
        api_key="sk-test",
        base_url="http://openai.test/v1",
        max_retries=0,
        http_client=httpx2.AsyncClient(transport=httpx2.MockTransport(handler)),
    )
    return OpenAILLM(client)


def request(**overrides: Any) -> LLMRequest:
    fields: dict[str, Any] = {
        "model": "gpt-test",
        "system": "Ты — консультант.",
        "messages": (UserMessage("Привет"),),
    }
    return LLMRequest(**(fields | overrides))


async def collect(llm: OpenAILLM, req: LLMRequest | None = None) -> list[LLMChunk]:
    return [item async for item in llm.stream(req or request())]


def completed(chunks: list[LLMChunk]) -> ResponseCompleted:
    last = chunks[-1]
    assert isinstance(last, ResponseCompleted)
    assert sum(isinstance(item, ResponseCompleted) for item in chunks) == 1
    return last


SEARCH = ToolSchema(
    name="search_catalog",
    description="Поиск по каталогу",
    parameters={"type": "object", "properties": {"query": {"type": "string"}}},
)


async def test_request_maps_messages_tools_and_options() -> None:
    server = Server(sse(chunk({"content": "ok"}, finish="stop")))
    call = ToolCall(
        id="call_1",
        name="search_catalog",
        arguments={"query": "крем"},
        raw_arguments='{"query": "крем"}',
    )
    req = request(
        messages=(
            UserMessage("Нужен крем"),
            AssistantMessage(text="", tool_calls=(call,)),
            ToolResultMessage(tool_call_id="call_1", content='{"items": []}'),
            AssistantMessage(text="Ничего нет."),
            UserMessage("Жаль"),
        ),
        tools=(SEARCH,),
        temperature=0.3,
        max_output_tokens=200,
    )

    await collect(llm_for(server), req)

    body = server.requests[0]
    assert body["model"] == "gpt-test"
    assert body["stream"] is True
    assert body["stream_options"] == {"include_usage": True}
    assert body["temperature"] == 0.3
    assert body["max_completion_tokens"] == 200
    assert body["messages"] == [
        {"role": "system", "content": "Ты — консультант."},
        {"role": "user", "content": "Нужен крем"},
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "call_1",
                    "type": "function",
                    "function": {"name": "search_catalog", "arguments": '{"query": "крем"}'},
                }
            ],
        },
        {"role": "tool", "tool_call_id": "call_1", "content": '{"items": []}'},
        {"role": "assistant", "content": "Ничего нет."},
        {"role": "user", "content": "Жаль"},
    ]
    assert body["tools"] == [
        {
            "type": "function",
            "function": {
                "name": "search_catalog",
                "description": "Поиск по каталогу",
                "parameters": SEARCH.parameters,
            },
        }
    ]


async def test_optional_fields_omitted() -> None:
    server = Server(sse(chunk({"content": "ok"}, finish="stop")))

    await collect(llm_for(server))

    body = server.requests[0]
    assert "tools" not in body
    assert "temperature" not in body
    assert "max_completion_tokens" not in body


async def test_text_stream_with_usage() -> None:
    server = Server(
        sse(
            chunk({"role": "assistant", "content": ""}),
            chunk({"content": "Здравствуйте"}),
            chunk({"content": ", чем помочь?"}),
            chunk({}, finish="stop"),
            chunk(usage={"prompt_tokens": 12, "completion_tokens": 5, "total_tokens": 17}),
        )
    )

    chunks = await collect(llm_for(server))

    assert chunks[:-1] == [TextDelta("Здравствуйте"), TextDelta(", чем помочь?")]
    response = completed(chunks).response
    assert response.text == "Здравствуйте, чем помочь?"
    assert response.tool_calls == ()
    assert response.stop_reason is StopReason.END_TURN
    assert response.usage == Usage(input_tokens=12, output_tokens=5)


async def test_parallel_tool_calls_assembled_from_fragments() -> None:
    server = Server(
        sse(
            chunk({"content": "Ищу"}),
            chunk(tool_fragment(0, id="call_a", name="search_catalog", arguments="")),
            chunk(tool_fragment(0, arguments='{"query"')),
            chunk(tool_fragment(1, id="call_b", name="get_entity")),
            chunk(tool_fragment(0, arguments=': "крем"}')),
            chunk(tool_fragment(1, arguments='{"id": 7}')),
            chunk({}, finish="tool_calls"),
        )
    )

    chunks = await collect(llm_for(server))

    assert chunks[:-1] == [
        TextDelta("Ищу"),
        ToolCallStarted(id="call_a", name="search_catalog"),
        ToolCallStarted(id="call_b", name="get_entity"),
    ]
    response = completed(chunks).response
    assert response.stop_reason is StopReason.TOOL_CALLS
    assert response.tool_calls == (
        ToolCall(
            id="call_a",
            name="search_catalog",
            arguments={"query": "крем"},
            raw_arguments='{"query": "крем"}',
        ),
        ToolCall(id="call_b", name="get_entity", arguments={"id": 7}, raw_arguments='{"id": 7}'),
    )


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("", {}),
        ('{"q": 1', None),
        ("[1, 2]", None),
        ('"text"', None),
    ],
)
async def test_tool_arguments_parsing(raw: str, expected: dict[str, Any] | None) -> None:
    server = Server(
        sse(
            chunk(tool_fragment(0, id="call_1", name="search_catalog", arguments=raw)),
            chunk({}, finish="tool_calls"),
        )
    )

    (call,) = completed(await collect(llm_for(server))).response.tool_calls

    assert call.arguments == expected
    assert call.raw_arguments == raw


@pytest.mark.parametrize(
    ("finish", "stop_reason"),
    [
        ("stop", StopReason.END_TURN),
        ("length", StopReason.MAX_TOKENS),
        ("content_filter", StopReason.END_TURN),
    ],
)
async def test_finish_reason_mapping(finish: str, stop_reason: StopReason) -> None:
    server = Server(sse(chunk({"content": "x"}, finish=finish)))

    response = completed(await collect(llm_for(server))).response

    assert response.stop_reason is stop_reason


async def test_tool_calls_with_stop_finish_reason_treated_as_tool_calls() -> None:
    server = Server(
        sse(
            chunk(tool_fragment(0, id="call_1", name="search_catalog", arguments="{}")),
            chunk({}, finish="stop"),
        )
    )

    response = completed(await collect(llm_for(server))).response

    assert response.stop_reason is StopReason.TOOL_CALLS


@pytest.mark.parametrize(
    ("status", "retryable"),
    [
        (400, False),
        (401, False),
        (403, False),
        (404, False),
        (408, True),
        (409, True),
        (429, True),
        (500, True),
        (503, True),
    ],
)
async def test_http_errors_mapped_to_llm_error(status: int, retryable: bool) -> None:
    llm = llm_for(Server(status=status))

    with pytest.raises(LLMError) as raised:
        await collect(llm)

    assert raised.value.retryable is retryable
    assert str(status) in str(raised.value)


async def test_connection_error_is_retryable() -> None:
    def refuse(request: httpx2.Request) -> httpx2.Response:
        raise httpx2.ConnectError("connection refused", request=request)

    with pytest.raises(LLMError) as raised:
        await collect(llm_for(refuse))

    assert raised.value.retryable


async def test_error_event_mid_stream_is_retryable_and_skips_completion() -> None:
    body = sse(chunk({"content": "Начало"}), done=False) + (
        'data: {"error": {"message": "server overloaded"}}\n\n'
    )
    received: list[LLMChunk] = []

    with pytest.raises(LLMError) as raised:
        async for item in llm_for(Server(body)).stream(request()):
            received.append(item)

    assert raised.value.retryable
    assert received == [TextDelta("Начало")]
