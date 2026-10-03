"""Адаптер Anthropic (P2-07) без сети: настоящий AsyncAnthropic поверх MockTransport с SSE."""

import json
from collections.abc import Callable, MutableMapping
from typing import Any

import httpx2
import pytest
from anthropic import AsyncAnthropic

from app.modules.agent.public import (
    AnthropicLLM,
    AssistantMessage,
    LLMChunk,
    LLMError,
    LLMRequest,
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
type Event = dict[str, Any]


def sse(*events: Event) -> str:
    return "".join(f"event: {event['type']}\ndata: {json.dumps(event)}\n\n" for event in events)


def message_start(input_tokens: int = 10, **cache: int) -> Event:
    return {
        "type": "message_start",
        "message": {
            "id": "msg_1",
            "type": "message",
            "role": "assistant",
            "model": "claude-test",
            "content": [],
            "stop_reason": None,
            "stop_sequence": None,
            "usage": {"input_tokens": input_tokens, "output_tokens": 1, **cache},
        },
    }


def text_start(index: int) -> Event:
    return {
        "type": "content_block_start",
        "index": index,
        "content_block": {"type": "text", "text": ""},
    }


def text_delta(index: int, text: str) -> Event:
    return {
        "type": "content_block_delta",
        "index": index,
        "delta": {"type": "text_delta", "text": text},
    }


def tool_start(index: int, id: str, name: str) -> Event:
    return {
        "type": "content_block_start",
        "index": index,
        "content_block": {"type": "tool_use", "id": id, "name": name, "input": {}},
    }


def json_delta(index: int, partial: str) -> Event:
    return {
        "type": "content_block_delta",
        "index": index,
        "delta": {"type": "input_json_delta", "partial_json": partial},
    }


def block_stop(index: int) -> Event:
    return {"type": "content_block_stop", "index": index}


def message_end(stop_reason: str = "end_turn", output_tokens: int = 5) -> list[Event]:
    return [
        {
            "type": "message_delta",
            "delta": {"stop_reason": stop_reason, "stop_sequence": None},
            "usage": {"output_tokens": output_tokens},
        },
        {"type": "message_stop"},
    ]


def text_reply(text: str = "ok", stop_reason: str = "end_turn") -> str:
    return sse(
        message_start(),
        text_start(0),
        text_delta(0, text),
        block_stop(0),
        *message_end(stop_reason),
    )


class Server:
    """Отвечает заданным SSE-телом (или статусом) и запоминает тела запросов."""

    def __init__(self, body: str = "", *, status: int = 200) -> None:
        self.body = body
        self.status = status
        self.requests: list[dict[str, Any]] = []

    def __call__(self, request: httpx2.Request) -> httpx2.Response:
        self.requests.append(json.loads(request.content))
        if self.status != 200:
            return httpx2.Response(
                self.status,
                json={"type": "error", "error": {"type": "api_error", "message": "boom"}},
            )
        return httpx2.Response(200, text=self.body, headers={"content-type": "text/event-stream"})


def llm_for(handler: Handler, *, default_max_tokens: int = 4096) -> AnthropicLLM:
    client = AsyncAnthropic(
        api_key="sk-ant-test",
        base_url="http://anthropic.test",
        max_retries=0,
        http_client=httpx2.AsyncClient(transport=httpx2.MockTransport(handler)),
    )
    return AnthropicLLM(client, default_max_tokens=default_max_tokens)


def request(**overrides: Any) -> LLMRequest:
    fields: dict[str, Any] = {
        "model": "claude-test",
        "system": "Ты — консультант.",
        "messages": (UserMessage("Привет"),),
    }
    return LLMRequest(**(fields | overrides))


async def collect(llm: AnthropicLLM, req: LLMRequest | None = None) -> list[LLMChunk]:
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


def call(id: str, arguments: dict[str, Any] | None, raw: str) -> ToolCall:
    return ToolCall(id=id, name="search_catalog", arguments=arguments, raw_arguments=raw)


async def test_request_maps_messages_tools_and_options() -> None:
    server = Server(text_reply())
    req = request(
        messages=(
            UserMessage("Нужен крем и сыворотка"),
            AssistantMessage(
                text="Ищу.",
                tool_calls=(
                    call("tu_1", {"query": "крем"}, '{"query": "крем"}'),
                    call("tu_2", {"query": "сыворотка"}, '{"query": "сыворотка"}'),
                ),
            ),
            ToolResultMessage(tool_call_id="tu_1", content='{"items": []}'),
            ToolResultMessage(tool_call_id="tu_2", content="timeout", is_error=True),
            AssistantMessage(text="Ничего нет."),
            UserMessage("Жаль"),
        ),
        tools=(SEARCH,),
        temperature=0.3,
        max_output_tokens=200,
    )

    await collect(llm_for(server), req)

    body = server.requests[0]
    assert body["model"] == "claude-test"
    assert body["stream"] is True
    assert body["system"] == "Ты — консультант."
    assert body["max_tokens"] == 200
    assert "temperature" not in body
    assert body["messages"] == [
        {"role": "user", "content": "Нужен крем и сыворотка"},
        {
            "role": "assistant",
            "content": [
                {"type": "text", "text": "Ищу."},
                {
                    "type": "tool_use",
                    "id": "tu_1",
                    "name": "search_catalog",
                    "input": {"query": "крем"},
                },
                {
                    "type": "tool_use",
                    "id": "tu_2",
                    "name": "search_catalog",
                    "input": {"query": "сыворотка"},
                },
            ],
        },
        {
            "role": "user",
            "content": [
                {"type": "tool_result", "tool_use_id": "tu_1", "content": '{"items": []}'},
                {
                    "type": "tool_result",
                    "tool_use_id": "tu_2",
                    "content": "timeout",
                    "is_error": True,
                },
            ],
        },
        {"role": "assistant", "content": "Ничего нет."},
        {"role": "user", "content": "Жаль"},
    ]
    assert body["tools"] == [
        {
            "name": "search_catalog",
            "description": "Поиск по каталогу",
            "input_schema": SEARCH.parameters,
        }
    ]


async def test_tool_use_without_text_and_with_unparsed_arguments() -> None:
    server = Server(text_reply())
    req = request(
        messages=(
            UserMessage("Нужен крем"),
            AssistantMessage(text="", tool_calls=(call("tu_1", None, '{"query"'),)),
            ToolResultMessage(tool_call_id="tu_1", content="bad arguments", is_error=True),
        )
    )

    await collect(llm_for(server), req)

    assistant = server.requests[0]["messages"][1]
    assert assistant["content"] == [
        {"type": "tool_use", "id": "tu_1", "name": "search_catalog", "input": {}}
    ]


async def test_optional_fields_omitted_and_default_max_tokens() -> None:
    server = Server(text_reply())

    await collect(llm_for(server, default_max_tokens=1024), request(system=""))

    body = server.requests[0]
    assert "tools" not in body
    assert "system" not in body
    assert body["max_tokens"] == 1024


async def test_text_stream_with_usage() -> None:
    server = Server(
        sse(
            message_start(input_tokens=12, cache_read_input_tokens=100),
            {"type": "ping"},
            text_start(0),
            text_delta(0, "Здравствуйте"),
            text_delta(0, ", чем помочь?"),
            block_stop(0),
            *message_end(output_tokens=7),
        )
    )

    chunks = await collect(llm_for(server))

    assert chunks[:-1] == [TextDelta("Здравствуйте"), TextDelta(", чем помочь?")]
    response = completed(chunks).response
    assert response.text == "Здравствуйте, чем помочь?"
    assert response.tool_calls == ()
    assert response.stop_reason is StopReason.END_TURN
    assert response.usage == Usage(input_tokens=112, output_tokens=7)


async def test_tool_calls_assembled_from_json_fragments() -> None:
    server = Server(
        sse(
            message_start(),
            text_start(0),
            text_delta(0, "Ищу"),
            block_stop(0),
            tool_start(1, "tu_a", "search_catalog"),
            json_delta(1, '{"query"'),
            json_delta(1, ': "крем"}'),
            block_stop(1),
            tool_start(2, "tu_b", "get_entity"),
            json_delta(2, '{"id": 7}'),
            block_stop(2),
            *message_end("tool_use"),
        )
    )

    chunks = await collect(llm_for(server))

    assert chunks[:-1] == [
        TextDelta("Ищу"),
        ToolCallStarted(id="tu_a", name="search_catalog"),
        ToolCallStarted(id="tu_b", name="get_entity"),
    ]
    response = completed(chunks).response
    assert response.stop_reason is StopReason.TOOL_CALLS
    assert response.tool_calls == (
        call("tu_a", {"query": "крем"}, '{"query": "крем"}'),
        ToolCall(id="tu_b", name="get_entity", arguments={"id": 7}, raw_arguments='{"id": 7}'),
    )


async def test_thinking_blocks_ignored() -> None:
    server = Server(
        sse(
            message_start(),
            {
                "type": "content_block_start",
                "index": 0,
                "content_block": {"type": "thinking", "thinking": "", "signature": ""},
            },
            {
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "thinking_delta", "thinking": "размышляю"},
            },
            block_stop(0),
            text_start(1),
            text_delta(1, "Ответ"),
            block_stop(1),
            *message_end(),
        )
    )

    chunks = await collect(llm_for(server))

    assert chunks[:-1] == [TextDelta("Ответ")]
    assert completed(chunks).response.text == "Ответ"


@pytest.mark.parametrize(
    ("partials", "expected", "raw"),
    [
        ((), {}, ""),
        (('{"q": 1',), None, '{"q": 1'),
        (("[1, 2]",), None, "[1, 2]"),
    ],
)
async def test_tool_arguments_parsing(
    partials: tuple[str, ...], expected: dict[str, Any] | None, raw: str
) -> None:
    server = Server(
        sse(
            message_start(),
            tool_start(0, "tu_1", "search_catalog"),
            *(json_delta(0, partial) for partial in partials),
            block_stop(0),
            *message_end("tool_use"),
        )
    )

    (result,) = completed(await collect(llm_for(server))).response.tool_calls

    assert result.arguments == expected
    assert result.raw_arguments == raw


@pytest.mark.parametrize(
    ("stop", "stop_reason"),
    [
        ("end_turn", StopReason.END_TURN),
        ("stop_sequence", StopReason.END_TURN),
        ("refusal", StopReason.END_TURN),
        ("max_tokens", StopReason.MAX_TOKENS),
        ("model_context_window_exceeded", StopReason.MAX_TOKENS),
    ],
)
async def test_stop_reason_mapping(stop: str, stop_reason: StopReason) -> None:
    response = completed(await collect(llm_for(Server(text_reply(stop_reason=stop))))).response

    assert response.stop_reason is stop_reason


async def test_tool_use_with_end_turn_treated_as_tool_calls() -> None:
    server = Server(
        sse(
            message_start(),
            tool_start(0, "tu_1", "search_catalog"),
            json_delta(0, "{}"),
            block_stop(0),
            *message_end("end_turn"),
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
        (413, False),
        (408, True),
        (409, True),
        (429, True),
        (500, True),
        (529, True),
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


@pytest.mark.parametrize(
    ("error_type", "retryable"),
    [
        ("overloaded_error", True),
        ("api_error", True),
        ("invalid_request_error", False),
    ],
)
async def test_error_event_mid_stream_skips_completion(error_type: str, retryable: bool) -> None:
    body = sse(message_start(), text_start(0), text_delta(0, "Начало")) + sse(
        {"type": "error", "error": {"type": error_type, "message": "boom"}}
    )
    received: list[LLMChunk] = []

    with pytest.raises(LLMError) as raised:
        async for item in llm_for(Server(body)).stream(request()):
            received.append(item)

    assert raised.value.retryable is retryable
    assert received == [TextDelta("Начало")]


async def test_dropped_temperature_is_logged(debug_logs: list[MutableMapping[str, Any]]) -> None:
    server = Server(text_reply())

    await collect(llm_for(server), request(temperature=0.3))
    await collect(llm_for(server), request())

    assert "temperature" not in server.requests[0]
    assert [e for e in debug_logs if e["event"] == "llm.param_dropped"] == [
        {
            "event": "llm.param_dropped",
            "log_level": "debug",
            "provider": "anthropic",
            "model": "claude-test",
            "param": "temperature",
        }
    ]
