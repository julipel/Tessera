"""Адаптер OpenAI Responses API (P2-11b, ADR-0010) без сети: настоящий AsyncOpenAI поверх
MockTransport с SSE-событиями Responses API."""

import json
from collections.abc import Callable, MutableMapping
from typing import Any

import httpx2
import pytest
from openai import AsyncOpenAI

from app.modules.agent.public import (
    AssistantMessage,
    LLMChunk,
    LLMError,
    LLMRequest,
    OpenAIResponsesLLM,
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


def sse(*events: dict[str, Any]) -> str:
    return "".join(
        f"event: {event['type']}\ndata: {json.dumps(event, ensure_ascii=False)}\n\n"
        for event in events
    )


def response(status: str = "completed", **fields: Any) -> dict[str, Any]:
    return {"id": "resp_1", "object": "response", "status": status, "output": []} | fields


def completed_event(input_tokens: int = 10, output_tokens: int = 5) -> dict[str, Any]:
    usage = {"input_tokens": input_tokens, "output_tokens": output_tokens}
    return {"type": "response.completed", "response": response(usage=usage)}


def text_delta(text: str) -> dict[str, Any]:
    return {"type": "response.output_text.delta", "item_id": "msg_1", "delta": text}


def function_call(index: int, call_id: str, name: str, arguments: str = "") -> dict[str, Any]:
    item = {"type": "function_call", "id": f"fc_{index}", "call_id": call_id, "name": name}
    return item | {"arguments": arguments}


def item_added(index: int, item: dict[str, Any]) -> dict[str, Any]:
    return {"type": "response.output_item.added", "output_index": index, "item": item}


def item_done(index: int, item: dict[str, Any]) -> dict[str, Any]:
    return {"type": "response.output_item.done", "output_index": index, "item": item}


# Форма reasoning item из output_item.done — как в документации Responses API.
REASONING = {
    "type": "reasoning",
    "id": "rs_1",
    "summary": [],
    "encrypted_content": "gAAAAB-зашифрованное-рассуждение==",
    "status": "completed",
}
REASONING_INPUT = {k: v for k, v in REASONING.items() if k != "status"}


def tool_call_stream() -> str:
    """Reasoning, затем вызов инструмента — как отвечает reasoning-модель."""
    call = function_call(1, "call_1", "search_catalog")
    return sse(
        item_added(0, REASONING | {"encrypted_content": None, "status": "in_progress"}),
        item_done(0, REASONING),
        item_added(1, call),
        {"type": "response.function_call_arguments.delta", "output_index": 1, "delta": "{}"},
        item_done(1, call | {"arguments": '{"query": "крем"}'}),
        completed_event(),
    )


class Server:
    """Отвечает заданным SSE-телом (или статусом) и запоминает тела запросов."""

    def __init__(self, body: str = "", *, status: int = 200) -> None:
        self.body = body
        self.status = status
        self.requests: list[dict[str, Any]] = []

    def __call__(self, request: httpx2.Request) -> httpx2.Response:
        assert request.url.path == "/v1/responses"
        self.requests.append(json.loads(request.content))
        if self.status != 200:
            return httpx2.Response(self.status, json={"error": {"message": "boom"}})
        return httpx2.Response(200, text=self.body, headers={"content-type": "text/event-stream"})


def llm_for(handler: Handler) -> OpenAIResponsesLLM:
    client = AsyncOpenAI(
        api_key="sk-test",
        base_url="http://openai.test/v1",
        max_retries=0,
        http_client=httpx2.AsyncClient(transport=httpx2.MockTransport(handler)),
    )
    return OpenAIResponsesLLM(client)


def request(**overrides: Any) -> LLMRequest:
    fields: dict[str, Any] = {
        "model": "gpt-test",
        "system": "Ты — консультант.",
        "messages": (UserMessage("Привет"),),
    }
    return LLMRequest(**(fields | overrides))


async def collect(llm: OpenAIResponsesLLM, req: LLMRequest | None = None) -> list[LLMChunk]:
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


def text_stream(text: str = "ok") -> str:
    return sse(text_delta(text), completed_event())


async def test_request_is_stateless_and_maps_messages_tools_and_options() -> None:
    server = Server(text_stream())
    call = ToolCall(
        id="call_1",
        name="search_catalog",
        arguments={"query": "крем"},
        raw_arguments='{"query": "крем"}',
    )
    req = request(
        messages=(
            UserMessage("Нужен крем"),
            AssistantMessage(text="Ищу.", tool_calls=(call,), provider_items=(REASONING_INPUT,)),
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
    assert body["store"] is False
    assert body["include"] == ["reasoning.encrypted_content"]
    assert "previous_response_id" not in body
    assert body["instructions"] == "Ты — консультант."
    assert body["temperature"] == 0.3
    assert body["max_output_tokens"] == 200
    assert body["input"] == [
        {"role": "user", "content": "Нужен крем"},
        REASONING_INPUT,  # данные провайдера — первыми, без изменений
        {"role": "assistant", "content": "Ищу."},
        {
            "type": "function_call",
            "call_id": "call_1",
            "name": "search_catalog",
            "arguments": '{"query": "крем"}',
        },
        {"type": "function_call_output", "call_id": "call_1", "output": '{"items": []}'},
        {"role": "assistant", "content": "Ничего нет."},
        {"role": "user", "content": "Жаль"},
    ]
    assert body["tools"] == [
        {
            "type": "function",
            "name": "search_catalog",
            "description": "Поиск по каталогу",
            "parameters": SEARCH.parameters,
            "strict": False,
        }
    ]


async def test_optional_fields_omitted() -> None:
    server = Server(text_stream())

    await collect(llm_for(server), request(system=""))

    body = server.requests[0]
    for field in ("instructions", "tools", "temperature", "max_output_tokens"):
        assert field not in body


async def test_text_stream_with_usage() -> None:
    body = sse(
        item_added(0, {"type": "message", "id": "msg_1", "role": "assistant", "content": []}),
        text_delta("При"),
        text_delta("вет"),
        {"type": "response.output_text.done", "item_id": "msg_1", "text": "Привет"},
        completed_event(input_tokens=12, output_tokens=3),
    )

    chunks = await collect(llm_for(Server(body)))

    assert chunks[:-1] == [TextDelta("При"), TextDelta("вет")]
    response_ = completed(chunks).response
    assert response_.text == "Привет"
    assert response_.stop_reason is StopReason.END_TURN
    assert response_.usage == Usage(12, 3)
    assert response_.provider_items == ()


async def test_refusal_is_streamed_as_text() -> None:
    body = sse({"type": "response.refusal.delta", "item_id": "msg_1", "delta": "Не могу."})

    chunks = await collect(llm_for(Server(body + sse(completed_event()))))

    assert completed(chunks).response.text == "Не могу."


async def test_tool_call_announced_early_and_reasoning_kept_as_provider_items() -> None:
    chunks = await collect(llm_for(Server(tool_call_stream())), request(tools=(SEARCH,)))

    assert chunks[0] == ToolCallStarted(id="call_1", name="search_catalog")
    response_ = completed(chunks).response
    assert response_.stop_reason is StopReason.TOOL_CALLS
    assert response_.tool_calls == (
        ToolCall(
            id="call_1",
            name="search_catalog",
            arguments={"query": "крем"},
            raw_arguments='{"query": "крем"}',
        ),
    )
    assert response_.provider_items == (REASONING_INPUT,)


async def test_reasoning_items_return_unchanged_in_next_step() -> None:
    """ADR-0010: reasoning item шага уходит во `input` следующего шага ровно как пришёл
    (без `status`) — перед вызовом инструмента."""
    server = Server(tool_call_stream())
    llm = llm_for(server)
    first = completed(await collect(llm, request(tools=(SEARCH,)))).response

    server.body = text_stream("Нашла.")
    messages = (
        UserMessage("Привет"),
        first.as_message(),
        ToolResultMessage(tool_call_id="call_1", content="[]"),
    )
    await collect(llm, request(messages=messages, tools=(SEARCH,)))

    assert server.requests[1]["input"] == [
        {"role": "user", "content": "Привет"},
        REASONING_INPUT,
        {
            "type": "function_call",
            "call_id": "call_1",
            "name": "search_catalog",
            "arguments": '{"query": "крем"}',
        },
        {"type": "function_call_output", "call_id": "call_1", "output": "[]"},
    ]


async def test_parallel_tool_calls_ordered_by_output_index() -> None:
    first, second = function_call(0, "call_a", "a"), function_call(1, "call_b", "b")
    body = sse(
        item_added(0, first),
        item_added(1, second),
        item_done(1, second | {"arguments": '{"x": 2}'}),
        item_done(0, first | {"arguments": "not json"}),
        completed_event(),
    )

    chunks = await collect(llm_for(Server(body)))

    assert chunks[:2] == [ToolCallStarted("call_a", "a"), ToolCallStarted("call_b", "b")]
    calls = completed(chunks).response.tool_calls
    assert [(c.id, c.arguments, c.raw_arguments) for c in calls] == [
        ("call_a", None, "not json"),  # цикл вернёт модели ошибку разбора
        ("call_b", {"x": 2}, '{"x": 2}'),
    ]


async def test_tool_call_without_arguments_gets_empty_object() -> None:
    call = function_call(0, "call_1", "list_stores")
    body = sse(item_added(0, call), item_done(0, call), completed_event())

    (parsed,) = completed(await collect(llm_for(Server(body)))).response.tool_calls

    assert parsed.arguments == {}


async def test_incomplete_by_max_output_tokens_is_max_tokens() -> None:
    incomplete = response(
        "incomplete",
        incomplete_details={"reason": "max_output_tokens"},
        usage={"input_tokens": 10, "output_tokens": 200},
    )
    body = sse(text_delta("Длинный"), {"type": "response.incomplete", "response": incomplete})

    response_ = completed(await collect(llm_for(Server(body)))).response

    assert response_.stop_reason is StopReason.MAX_TOKENS
    assert response_.text == "Длинный"
    assert response_.usage == Usage(10, 200)


async def test_incomplete_for_other_reason_ends_turn() -> None:
    incomplete = response("incomplete", incomplete_details={"reason": "content_filter"})
    body = sse(text_delta("Часть"), {"type": "response.incomplete", "response": incomplete})

    response_ = completed(await collect(llm_for(Server(body)))).response

    assert response_.stop_reason is StopReason.END_TURN
    assert response_.text == "Часть"


@pytest.mark.parametrize(
    "event",
    [
        {
            "type": "response.failed",
            "response": response("failed", error={"code": "server_error", "message": "overloaded"}),
        },
        {"type": "error", "code": "server_error", "message": "overloaded", "param": None},
    ],
    ids=["failed", "error"],
)
async def test_failure_mid_stream_is_retryable_and_skips_completion(event: dict[str, Any]) -> None:
    received: list[LLMChunk] = []

    with pytest.raises(LLMError) as raised:
        async for item in llm_for(Server(sse(text_delta("Начало"), event))).stream(request()):
            received.append(item)

    assert raised.value.retryable
    assert "overloaded" in str(raised.value)
    assert received == [TextDelta("Начало")]


@pytest.mark.parametrize(
    ("status", "retryable"),
    [(400, False), (401, False), (404, False), (408, True), (429, True), (500, True)],
)
async def test_http_errors_mapped_to_llm_error(status: int, retryable: bool) -> None:
    with pytest.raises(LLMError) as raised:
        await collect(llm_for(Server(status=status)))

    assert raised.value.retryable is retryable
    assert str(status) in str(raised.value)


async def test_connection_error_is_retryable() -> None:
    def refuse(request: httpx2.Request) -> httpx2.Response:
        raise httpx2.ConnectError("connection refused", request=request)

    with pytest.raises(LLMError) as raised:
        await collect(llm_for(refuse))

    assert raised.value.retryable


# --- temperature: адаптивный отказ (ADR-0009) ---

UNSUPPORTED_TEMPERATURE = {
    "error": {
        "message": "Unsupported parameter: 'temperature' is not supported with this model.",
        "type": "invalid_request_error",
        "param": "temperature",
        "code": "unsupported_parameter",
    }
}


class RejectsTemperature(Server):
    """Модели из `strict` отвечают 400 на запрос с temperature — как reasoning-модели OpenAI."""

    def __init__(self, *strict: str, error: dict[str, Any] = UNSUPPORTED_TEMPERATURE) -> None:
        super().__init__(text_stream())
        self.strict = strict
        self.error = error

    def __call__(self, request: httpx2.Request) -> httpx2.Response:
        response_ = super().__call__(request)
        body = self.requests[-1]
        if body["model"] in self.strict and "temperature" in body:
            return httpx2.Response(400, json=self.error)
        return response_


def dropped(logs: list[MutableMapping[str, Any]]) -> list[tuple[str, str]]:
    return [(e["provider"], e["model"]) for e in logs if e["event"] == "llm.param_dropped"]


async def test_rejected_temperature_is_retried_without_it_and_remembered(
    debug_logs: list[MutableMapping[str, Any]],
) -> None:
    server = RejectsTemperature("gpt-reasoning")
    llm = llm_for(server)

    first = await collect(llm, request(model="gpt-reasoning", temperature=0.3))
    await collect(llm, request(model="gpt-reasoning", temperature=0.3))
    await collect(llm, request(model="gpt-test", temperature=0.3))

    assert completed(first).response.text == "ok"
    assert [(b["model"], b.get("temperature")) for b in server.requests] == [
        ("gpt-reasoning", 0.3),
        ("gpt-reasoning", None),  # повтор без temperature
        ("gpt-reasoning", None),  # модель запомнена — без лишнего 400
        ("gpt-test", 0.3),
    ]
    assert dropped(debug_logs) == [("openai", "gpt-reasoning"), ("openai", "gpt-reasoning")]


@pytest.mark.parametrize(
    "error",
    [
        {"error": {"message": "bad", "type": "invalid_request_error", "param": "input"}},
        {"error": {"message": "bad", "param": "temperature", "code": "invalid_type"}},
    ],
)
async def test_other_bad_requests_are_not_retried(error: dict[str, Any]) -> None:
    server = RejectsTemperature("gpt-test", error=error)

    with pytest.raises(LLMError) as raised:
        await collect(llm_for(server), request(temperature=0.3))

    assert raised.value.retryable is False
    assert len(server.requests) == 1
