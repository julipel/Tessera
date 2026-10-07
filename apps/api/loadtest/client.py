"""Посетитель чата: создаёт диалог и делает ходы подряд, замеряя SSE-стрим каждого хода."""

import asyncio
import json
import time
import uuid
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass, field

import httpx

from loadtest.stats import TurnSample


@dataclass(frozen=True)
class Scenario:
    widget_key: str
    turns: int
    text: str
    locale: str = "ru-RU"


@dataclass
class VisitorResult:
    create_s: float | None = None
    create_error: str | None = None
    turns: list[TurnSample] = field(default_factory=list)


@dataclass(frozen=True)
class SseEvent:
    type: str
    data: dict[str, object]


async def sse_events(lines: AsyncIterator[str]) -> AsyncIterator[SseEvent]:
    """События SSE: `event:` + `data:` до пустой строки; комментарии (`:`) пропускаются."""
    kind, data = "", ""
    async for line in lines:
        if not line:
            if data:
                yield SseEvent(kind or "message", json.loads(data))
            kind, data = "", ""
        elif line.startswith("event:"):
            kind = line.removeprefix("event:").strip()
        elif line.startswith("data:"):
            data += line.removeprefix("data:").strip()
    if data:
        yield SseEvent(kind or "message", json.loads(data))


async def run_visitor(client: httpx.AsyncClient, scenario: Scenario) -> VisitorResult:
    headers = {"X-Widget-Key": scenario.widget_key}
    result = VisitorResult()
    start = time.perf_counter()
    try:
        response = await client.post(
            "/v1/conversations",
            json={"visitor_id": str(uuid.uuid4()), "locale": scenario.locale},
            headers=headers,
        )
    except httpx.HTTPError as exc:
        result.create_error = f"exc_{type(exc).__name__}"
        return result
    if response.status_code != 201:
        result.create_error = f"http_{response.status_code}"
        return result
    result.create_s = time.perf_counter() - start
    conversation_id = response.json()["conversation_id"]
    for index in range(scenario.turns):
        text = f"{scenario.text} ({index + 1})"
        result.turns.append(await run_turn(client, conversation_id, text, headers))
    return result


async def run_turn(
    client: httpx.AsyncClient, conversation_id: str, text: str, headers: dict[str, str]
) -> TurnSample:
    body = {"client_message_id": str(uuid.uuid4()), "input": {"type": "text", "text": text}}
    start = time.perf_counter()
    started: float | None = None
    first_token: float | None = None
    error_code: str | None = None
    outcome = "no_done"
    try:
        async with client.stream(
            "POST", f"/v1/conversations/{conversation_id}/messages", json=body, headers=headers
        ) as response:
            if response.status_code != 200:
                await response.aread()
                outcome = f"http_{response.status_code}"
            else:
                async for event in sse_events(response.aiter_lines()):
                    elapsed = time.perf_counter() - start
                    if event.type == "turn_started" and started is None:
                        started = elapsed
                    elif event.type == "text_delta" and first_token is None:
                        first_token = elapsed
                    elif event.type == "error":
                        error_code = str(_data(event).get("code"))
                    elif event.type == "done":
                        outcome = str(_data(event).get("status"))
                        break
    except httpx.HTTPError as exc:
        outcome = f"exc_{type(exc).__name__}"
    if error_code is not None and outcome in ("failed", "no_done"):
        outcome = f"error_{error_code}"
    return TurnSample(started, first_token, time.perf_counter() - start, outcome)


def _data(event: SseEvent) -> dict[str, object]:
    data = event.data.get("data")
    return data if isinstance(data, dict) else {}


async def run_load(
    client: httpx.AsyncClient,
    scenario: Scenario,
    *,
    dialogs: int,
    concurrency: int,
    ramp_s: float = 0.0,
) -> Sequence[VisitorResult]:
    """`dialogs` посетителей, не больше `concurrency` одновременно.

    Старты — равномерно за `ramp_s` секунд.
    """
    gate = asyncio.Semaphore(concurrency)

    async def visitor(index: int) -> VisitorResult:
        if ramp_s > 0:
            await asyncio.sleep(ramp_s * index / dialogs)
        async with gate:
            return await run_visitor(client, scenario)

    return await asyncio.gather(*(visitor(i) for i in range(dialogs)))
