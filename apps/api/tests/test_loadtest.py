"""Нагрузочный тест (loadtest/, P7-05): разбор SSE, исходы ходов, сводка — без живого API."""

import json
from collections.abc import AsyncIterator, Callable

import httpx
import pytest

from loadtest.client import Scenario, run_load, run_turn, sse_events
from loadtest.stats import TurnSample, percentile, render, summarize

CONVERSATION = "c-1"
HEADERS = {"X-Widget-Key": "wk"}


def sse(*events: tuple[str, dict[str, object]]) -> bytes:
    return "".join(
        f"event: {kind}\ndata: {json.dumps({'type': kind, 'data': data})}\n\n"
        for kind, data in events
    ).encode()


COMPLETED = sse(
    ("turn_started", {}),
    ("text_delta", {"block_id": "b1", "delta": "Вы "}),
    ("text_delta", {"block_id": "b1", "delta": "написали"}),
    ("text_done", {"block_id": "b1"}),
    ("done", {"status": "completed", "usage": {}}),
)


def client_for(handler: Callable[[httpx.Request], httpx.Response]) -> httpx.AsyncClient:
    return httpx.AsyncClient(base_url="http://api", transport=httpx.MockTransport(handler))


async def lines(*items: str) -> AsyncIterator[str]:
    for item in items:
        yield item


async def test_sse_events_parses_event_and_data_skipping_comments() -> None:
    raw = [": ping", "", "event: text_delta", 'data: {"seq": 1}', "", 'data: {"seq": 2}']
    events = [e async for e in sse_events(lines(*raw))]
    assert [(e.type, e.data) for e in events] == [
        ("text_delta", {"seq": 1}),
        ("message", {"seq": 2}),
    ]


async def test_turn_measures_started_first_token_and_completion() -> None:
    async with client_for(lambda _: httpx.Response(200, content=COMPLETED)) as client:
        sample = await run_turn(client, CONVERSATION, "Привет", HEADERS)
    assert sample.outcome == "completed"
    assert sample.started_s is not None and sample.first_token_s is not None
    assert sample.started_s <= sample.first_token_s <= sample.duration_s


async def test_turn_without_text_has_no_first_token() -> None:
    body = sse(("turn_started", {}), ("done", {"status": "completed", "usage": {}}))
    async with client_for(lambda _: httpx.Response(200, content=body)) as client:
        sample = await run_turn(client, CONVERSATION, "Привет", HEADERS)
    assert (sample.outcome, sample.first_token_s) == ("completed", None)


@pytest.mark.parametrize(
    ("response", "outcome"),
    [
        (httpx.Response(429, json={"error": {"code": "rate_limited"}}), "http_429"),
        (
            httpx.Response(
                200,
                content=sse(
                    ("turn_started", {}),
                    ("error", {"code": "llm_unavailable", "retryable": True}),
                    ("done", {"status": "failed", "usage": {}}),
                ),
            ),
            "error_llm_unavailable",
        ),
        (httpx.Response(200, content=sse(("turn_started", {}))), "no_done"),
    ],
)
async def test_turn_outcomes(response: httpx.Response, outcome: str) -> None:
    async with client_for(lambda _: response) as client:
        sample = await run_turn(client, CONVERSATION, "Привет", HEADERS)
    assert sample.outcome == outcome


async def test_turn_network_error_is_an_outcome() -> None:
    def fail(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("таймаут", request=request)

    async with client_for(fail) as client:
        sample = await run_turn(client, CONVERSATION, "Привет", HEADERS)
    assert sample.outcome == "exc_ReadTimeout"


async def test_load_creates_dialogs_and_runs_turns_in_each() -> None:
    requests: list[httpx.Request] = []

    def api(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        assert request.headers["X-Widget-Key"] == "wk"
        if request.url.path == "/v1/conversations":
            return httpx.Response(201, json={"conversation_id": CONVERSATION})
        assert request.url.path == f"/v1/conversations/{CONVERSATION}/messages"
        return httpx.Response(200, content=COMPLETED)

    scenario = Scenario(widget_key="wk", turns=2, text="Вопрос")
    async with client_for(api) as client:
        visitors = await run_load(client, scenario, dialogs=3, concurrency=2)
    assert len(visitors) == 3
    assert all(v.create_s is not None and len(v.turns) == 2 for v in visitors)
    turns = [r for r in requests if r.url.path.endswith("/messages")]
    texts = {json.loads(r.content)["input"]["text"] for r in turns}
    assert len(turns) == 6
    assert {"Вопрос (1)", "Вопрос (2)"} <= texts


async def test_failed_conversation_is_reported_without_turns() -> None:
    async with client_for(lambda _: httpx.Response(429)) as client:
        [visitor] = await run_load(client, Scenario("wk", 3, "Вопрос"), dialogs=1, concurrency=1)
    assert (visitor.create_error, visitor.turns) == ("http_429", [])


def test_percentile_nearest_rank() -> None:
    values = [float(v) for v in range(1, 101)]
    assert [percentile(values, q) for q in (50, 95, 99, 100)] == [50, 95, 99, 100]
    assert percentile([3.0], 99) == 3.0
    with pytest.raises(ValueError, match="пустая"):
        percentile([], 50)


def test_summary_counts_latency_only_for_completed_turns() -> None:
    turns = [
        TurnSample(0.1, 0.5, 1.0, "completed"),
        TurnSample(0.2, 0.7, 2.0, "completed"),
        TurnSample(0.1, None, 30.0, "error_llm_unavailable"),
        TurnSample(None, None, 0.01, "http_429"),
    ]
    summary = summarize(turns, [0.05, 0.07], ["http_429"], wall_s=4.0)
    assert (summary.turns, summary.completed, summary.error_rate) == (4, 2, 0.5)
    assert summary.turns_per_s == 0.5
    assert summary.duration is not None and summary.duration.max == 2.0
    assert summary.first_token is not None and summary.first_token.p50 == 0.5
    assert summary.outcomes == {"completed": 2, "error_llm_unavailable": 1, "http_429": 1}
    text = render(summary)
    assert "до первого токена" in text and "диалог не создан: http_429=1" in text


def test_summary_of_empty_run() -> None:
    summary = summarize([], [], [], wall_s=0.0)
    assert (summary.error_rate, summary.first_token) == (0.0, None)
    assert "—" in render(summary)
