"""Трейсинг ходов (P7-03, ADR-0029): generation на каждую попытку модели, инструменты,
итог хода; адаптер Langfuse — на in-memory экспортёре OpenTelemetry."""

import asyncio
import json
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest
from fastapi import FastAPI
from httpx import AsyncClient
from langfuse import Langfuse
from opentelemetry.sdk.trace import ReadableSpan, TracerProvider
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.agent.application import fallback_llm
from app.modules.agent.public import (
    PLATFORM_PROMPT_VERSION,
    AgentEvent,
    AnswerDelta,
    FakeLLM,
    FakeReply,
    FallbackLLM,
    FallbackModel,
    FinishReason,
    LLMError,
    LLMRequest,
    Provider,
    RegistryToolExecutor,
    ToolCall,
    ToolFinished,
    ToolStarted,
    TracedLLM,
    TurnCompleted,
    Usage,
    UserMessage,
    traced_turn,
)
from app.modules.chat.domain.entities import TurnRequest
from app.modules.chat.infrastructure.loop_agent import LoopTurnAgent
from app.modules.observability.infrastructure.langfuse_tracer import _guard, usage_details
from app.modules.observability.kernel import (
    GenerationInfo,
    GenerationTrace,
    GenerationUsage,
    TurnTraceInfo,
)
from app.modules.observability.public import LangfuseTracer, NoopTracer, build_tracer
from app.modules.observability.public import langfuse_trace_id as lf_trace_id
from app.modules.shared.kernel import TenantId
from app.modules.tools.public import ToolRegistry
from test_agent_events import _tenant

TENANT = TenantId(uuid4())


@dataclass
class FakeGeneration:
    info: GenerationInfo
    first_chunks: int = 0
    output: dict[str, Any] | None = None
    usage: GenerationUsage | None = None
    error: str | None = None

    def first_chunk(self) -> None:
        self.first_chunks += 1

    def finish(self, output: dict[str, Any], usage: GenerationUsage) -> None:
        self.output, self.usage = output, usage

    def fail(self, error: str, usage: GenerationUsage | None = None) -> None:
        self.error, self.usage = error, usage


@dataclass
class FakeTurn:
    info: TurnTraceInfo
    generations: list[FakeGeneration] = field(default_factory=list)
    tools: list[tuple[str, ...] | tuple[Any, ...]] = field(default_factory=list)
    output: dict[str, Any] | None = None
    error: str | None = None
    finished: int = 0

    def generation(self, info: GenerationInfo) -> GenerationTrace:
        generation = FakeGeneration(info)
        self.generations.append(generation)
        return generation

    def tool_started(self, call_id: str, name: str) -> None:
        self.tools.append(("started", call_id, name))

    def tool_finished(
        self,
        call_id: str,
        name: str,
        arguments: Any,
        output: Any,
        error: str | None,
        duration_ms: int,
    ) -> None:
        self.tools.append(("finished", call_id, name, arguments, output, error))

    def finish(self, output: dict[str, Any], error: str | None = None) -> None:
        self.output, self.error = output, error
        self.finished += 1


@dataclass
class FakeTracer:
    turns: list[FakeTurn] = field(default_factory=list)

    def start_turn(self, info: TurnTraceInfo) -> FakeTurn:
        turn = FakeTurn(info)
        self.turns.append(turn)
        return turn

    def shutdown(self) -> None:
        pass


def _info(trace_id: str = "") -> TurnTraceInfo:
    return TurnTraceInfo(
        trace_id=trace_id,
        tenant_id=TENANT,
        conversation_id=uuid4(),
        turn_id=uuid4(),
        input={"type": "text", "text": "Привет"},
        prompt_versions={"platform_prompt": "1", "agent_config": "cfg-1"},
    )


REQUEST = LLMRequest(model="main", system="Ты — консультант.", messages=(UserMessage("Привет"),))


async def _drain(llm: Any, request: LLMRequest = REQUEST) -> None:
    async for _ in llm.stream(request):
        pass


async def test_traced_llm_records_generation_with_usage_and_first_chunk() -> None:
    turn = FakeTurn(_info())
    usage = Usage(30, 2, cache_read_tokens=20, cache_write_tokens=5)
    llm = TracedLLM(FakeLLM([FakeReply("Добрый день", usage=usage)]), turn, "anthropic")

    await _drain(llm, REQUEST)

    [generation] = turn.generations
    assert (generation.info.model, generation.info.provider) == ("main", "anthropic")
    assert generation.info.input == {
        "system": "Ты — консультант.",
        "messages": [{"role": "user", "content": "Привет"}],
    }
    assert generation.first_chunks >= 1
    assert generation.output == {"text": "Добрый день", "tool_calls": [], "stop_reason": "end_turn"}
    assert generation.usage == GenerationUsage(30, 2, cache_read_tokens=20, cache_write_tokens=5)
    assert generation.error is None


async def test_llm_error_usage_reaches_failed_generation() -> None:
    """Токены до обрыва провайдер тарифицирует — они в трейсе и у неудачной попытки (O-7)."""
    turn = FakeTurn(_info())
    error = LLMError("обрыв", retryable=True, usage=Usage(40, 3))
    llm = TracedLLM(FakeLLM([error]), turn, "anthropic")

    with pytest.raises(LLMError):
        await _drain(llm)

    [generation] = turn.generations
    assert generation.error == "LLMError: обрыв"
    assert generation.usage == GenerationUsage(40, 3)


@pytest.mark.parametrize(
    ("provider", "expected"),
    [
        (
            "anthropic",
            {
                "input": 25,
                "cache_read_input_tokens": 60,
                "cache_creation_input_tokens": 15,
                "output": 7,
            },
        ),
        ("openai", {"input": 25, "input_cached_tokens": 75, "output": 7}),
        ("openai_compatible", {"input": 25, "input_cached_tokens": 75, "output": 7}),
    ],
)
def test_usage_details_are_disjoint_buckets(provider: str, expected: dict[str, int]) -> None:
    """Langfuse складывает корзины: вход без кэша + кэш = весь вход шага, без двойного счёта."""
    usage = GenerationUsage(100, 7, cache_read_tokens=60, cache_write_tokens=15)

    details = usage_details(provider, usage)

    assert details == expected
    assert sum(v for k, v in details.items() if k != "output") == usage.input_tokens


def test_usage_details_without_cache_are_input_and_output() -> None:
    assert usage_details("anthropic", GenerationUsage(12, 3)) == {"input": 12, "output": 3}
    assert usage_details("openai", GenerationUsage(12, 3)) == {"input": 12, "output": 3}


async def test_each_retry_and_fallback_attempt_is_a_generation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(fallback_llm, "RETRY_DELAY_S", 0)
    turn = FakeTurn(_info())
    flaky = LLMError("503", retryable=True)
    primary = TracedLLM(FakeLLM([flaky, flaky]), turn, "anthropic")
    backup = TracedLLM(FakeLLM([FakeReply("Ответ")]), turn, "openai")
    llm = FallbackLLM(primary, FallbackModel(client=lambda: backup, model="backup"))

    await _drain(llm)

    assert [(g.info.model, g.info.provider, g.error is None) for g in turn.generations] == [
        ("main", "anthropic", False),
        ("main", "anthropic", False),
        ("backup", "openai", True),
    ]
    assert turn.generations[0].error == "LLMError: 503"


async def test_stream_failure_after_first_chunk_fails_generation() -> None:
    turn = FakeTurn(_info())
    broken = FakeReply("раз два три", fail_after_chunks=1)
    llm = TracedLLM(FakeLLM([broken]), turn, "openai")

    with pytest.raises(LLMError):
        await _drain(llm)

    [generation] = turn.generations
    assert generation.first_chunks == 1 and generation.output is None
    assert generation.error is not None


async def _events(*events: AgentEvent | Exception) -> AsyncIterator[AgentEvent]:
    for event in events:
        if isinstance(event, Exception):
            raise event
        yield event


async def test_traced_turn_records_tools_and_result() -> None:
    turn = FakeTurn(_info())
    events = _events(
        ToolStarted("c1", "search_catalog"),
        ToolFinished("c1", "search_catalog", ok=True, arguments={"q": "крем"}, content={"n": 1}),
        ToolStarted("c2", "get_entity"),
        ToolFinished(
            "c2", "get_entity", ok=False, error_code="not_found", error_message="нет такого"
        ),
        AnswerDelta("Вот "),
        AnswerDelta("крем."),
        TurnCompleted(FinishReason.ANSWERED, Usage(40, 5), steps=2),
    )

    passed = [e async for e in traced_turn(events, turn)]

    assert len(passed) == 7
    assert turn.tools == [
        ("started", "c1", "search_catalog"),
        ("finished", "c1", "search_catalog", {"q": "крем"}, {"n": 1}, None),
        ("started", "c2", "get_entity"),
        ("finished", "c2", "get_entity", None, "", "not_found: нет такого"),
    ]
    assert turn.output == {
        "answer": "Вот крем.",
        "finish": "answered",
        "steps": 2,
        "input_tokens": 40,
        "output_tokens": 5,
    }
    assert turn.error is None and turn.finished == 1


async def test_traced_turn_finishes_with_error_and_on_cancel() -> None:
    failed = FakeTurn(_info())
    with pytest.raises(LLMError):
        async for _ in traced_turn(
            _events(AnswerDelta("Сей"), LLMError("503", retryable=True)), failed
        ):
            pass
    assert (failed.output, failed.error) == ({"answer": "Сей"}, "LLMError: 503")

    interrupted = FakeTurn(_info())
    stream = traced_turn(_events(AnswerDelta("Под"), AnswerDelta("бираю")), interrupted)
    await anext(stream)
    await stream.aclose()
    assert (interrupted.error, interrupted.finished) == ("прерван", 1)


def _request(trace_id: str) -> TurnRequest:
    return TurnRequest(
        tenant_id=TENANT,
        conversation_id=uuid4(),
        agent_config_id=uuid4(),
        turn_id=uuid4(),
        input={"type": "text", "text": "Привет"},
        agent_config={
            "assistant": {"name": "A", "greeting": "Привет!", "fallback_message": "Не вышло."},
            "model": {"primary": {"provider": "openai", "name": "main-model"}},
            "limits": {},
            "prompt": {"tenant": "Ты — консультант."},
            "tools": {},
        },
        history=(),
        trace_id=trace_id,
    )


async def test_loop_agent_traces_turn_with_trace_id_and_prompt_versions() -> None:
    tracer = FakeTracer()
    llm = FakeLLM(
        [
            FakeReply(tool_calls=(ToolCall("c1", "nope", {}, "{}"),)),
            FakeReply("Готово.", usage=Usage(10, 2)),
        ]
    )

    def llm_for(_: Provider) -> FakeLLM:
        return llm

    agent = LoopTurnAgent(llm_for, lambda *_: RegistryToolExecutor(ToolRegistry([])), tracer=tracer)
    request = _request("trace-loop")
    events = [e async for e in agent.run_turn(request)]

    [turn] = tracer.turns
    assert (turn.info.trace_id, turn.info.turn_id) == ("trace-loop", request.turn_id)
    assert turn.info.prompt_versions == {
        "platform_prompt": PLATFORM_PROMPT_VERSION,
        "agent_config": str(request.agent_config_id),
    }
    assert [(g.info.model, g.info.provider) for g in turn.generations] == [
        ("main-model", "openai"),
        ("main-model", "openai"),
    ]
    # Неизвестный инструмент — ошибка вызова в трейсе, ход продолжается.
    assert turn.tools[0] == ("started", "c1", "nope")
    assert turn.tools[1][:3] == ("finished", "c1", "nope") and turn.tools[1][5] is not None
    assert turn.output is not None and turn.output["answer"] == "Готово."
    assert isinstance(events[-1], TurnCompleted)


async def test_loop_agent_passes_fallback_provider_to_its_generations(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Провайдер резервной модели — свой: от него зависят корзины токенов (O-7)."""
    monkeypatch.setattr(fallback_llm, "RETRY_DELAY_S", 0)
    tracer = FakeTracer()
    flaky = LLMError("503", retryable=True)
    clients = {"anthropic": FakeLLM([flaky, flaky, flaky]), "openai": FakeLLM([FakeReply("Да")])}
    agent = LoopTurnAgent(
        lambda provider: clients[provider],
        lambda *_: RegistryToolExecutor(ToolRegistry([])),
        tracer=tracer,
    )
    request = _request("trace-fallback")
    request.agent_config["model"] = {
        "primary": {"provider": "anthropic", "name": "main-model"},
        "fallback": {"provider": "openai", "name": "backup-model"},
    }

    [e async for e in agent.run_turn(request)]

    [turn] = tracer.turns
    providers = [(g.info.model, g.info.provider) for g in turn.generations]
    assert providers[-1] == ("backup-model", "openai")
    assert set(providers[:-1]) == {("main-model", "anthropic")}


def test_build_tracer_without_keys_is_noop() -> None:
    assert isinstance(build_tracer(None, None, None, "test"), NoopTracer)
    assert isinstance(build_tracer("pk", None, None, "test"), NoopTracer)


def test_langfuse_trace_id_keeps_otel_ids_and_derives_others() -> None:
    hex_id = uuid4().hex
    assert lf_trace_id(hex_id) == hex_id
    derived = lf_trace_id("from-header")
    assert derived == lf_trace_id("from-header") and len(derived) == 32 and derived != hex_id
    assert len(lf_trace_id("")) == 32


def _attrs(span: ReadableSpan) -> dict[str, Any]:
    return dict(span.attributes or {})


def test_langfuse_tracer_exports_turn_generation_and_tool_spans() -> None:
    exporter = InMemorySpanExporter()
    client = Langfuse(
        public_key=f"pk-lf-test-{uuid4().hex}",  # клиенты SDK кэшируются по ключу
        secret_key="sk-lf-test",
        base_url="http://localhost:9",
        tracer_provider=TracerProvider(),
        span_exporter=exporter,
        environment="test",
    )
    tracer = LangfuseTracer(client)
    trace_hex = uuid4().hex
    info = _info(trace_hex)

    turn = tracer.start_turn(info)
    generation = turn.generation(
        GenerationInfo(name="llm", model="main", input={"system": "s", "messages": []})
    )
    generation.first_chunk()
    generation.finish({"text": "Да"}, GenerationUsage(12, 3))
    turn.tool_started("c1", "search_catalog")
    turn.tool_finished("c1", "search_catalog", {"q": "крем"}, {"n": 1}, None, 5)
    turn.finish({"answer": "Да"})
    client.flush()

    spans = {s.name: s for s in exporter.get_finished_spans()}
    assert set(spans) == {"turn", "llm", "search_catalog"}
    assert {f"{s.context.trace_id:032x}" for s in spans.values()} == {trace_hex}
    root, llm, tool = _attrs(spans["turn"]), _attrs(spans["llm"]), _attrs(spans["search_catalog"])
    assert root["langfuse.observation.type"] == "agent"
    assert llm["langfuse.observation.type"] == "generation"
    assert tool["langfuse.observation.type"] == "tool"
    assert llm["langfuse.observation.model.name"] == "main"
    assert json.loads(str(llm["langfuse.observation.usage_details"])) == {"input": 12, "output": 3}
    assert "langfuse.observation.completion_start_time" in llm
    for attrs in (root, llm, tool):
        assert attrs["session.id"] == str(info.conversation_id)
        assert attrs["langfuse.version"] == "platform_prompt:1;agent_config:cfg-1"
    client.shutdown()


async def test_http_turn_is_traced_with_request_trace_id(
    app: FastAPI, db_client: AsyncClient, db_session: AsyncSession
) -> None:
    await _tenant(db_session, "shop")
    tracer, llm = FakeTracer(), FakeLLM([FakeReply("Здравствуйте!")])
    app.state.tracer = tracer
    app.state.llm_clients = SimpleNamespace(for_provider=lambda _: llm)
    created = await db_client.post(
        "/v1/conversations", json={"visitor_id": "v-1"}, headers={"X-Widget-Key": "wk_shop"}
    )
    conversation_id = created.json()["conversation_id"]

    await db_client.post(
        f"/v1/conversations/{conversation_id}/messages",
        json={"client_message_id": str(uuid4()), "input": {"type": "text", "text": "Привет"}},
        headers={"X-Widget-Key": "wk_shop", "X-Trace-Id": "trace-http"},
    )

    [turn] = tracer.turns
    assert (turn.info.trace_id, str(turn.info.conversation_id)) == ("trace-http", conversation_id)
    assert turn.output is not None and turn.output["answer"] == "Здравствуйте!"


def _langfuse(exporter: InMemorySpanExporter) -> Langfuse:
    return Langfuse(
        public_key=f"pk-lf-test-{uuid4().hex}",
        secret_key="sk-lf-test",
        base_url="http://localhost:9",
        tracer_provider=TracerProvider(),
        span_exporter=exporter,
        environment="test",
        mask=lambda *, data, **_: data,
    )


def test_prompt_versions_do_not_overwrite_trace_metadata() -> None:
    """O-11: версии промпта — одним ключом, `tenant_id` слоя версий не затирает тенанта."""
    exporter = InMemorySpanExporter()
    client = _langfuse(exporter)
    info = TurnTraceInfo(
        trace_id="",
        tenant_id=TENANT,
        conversation_id=uuid4(),
        turn_id=uuid4(),
        input={},
        prompt_versions={"tenant_id": "чужой", "turn_id": "x", "platform_prompt": "1"},
    )
    turn = LangfuseTracer(client).start_turn(info)
    turn.finish({"answer": ""})
    client.flush()

    [root] = exporter.get_finished_spans()
    attrs = _attrs(root)
    assert attrs["langfuse.trace.metadata.tenant_id"] == str(TENANT)
    assert attrs["langfuse.trace.metadata.turn_id"] == str(info.turn_id)
    assert json.loads(str(attrs["langfuse.trace.metadata.prompt_versions"])) == (
        info.prompt_versions
    )
    client.shutdown()


def test_failed_generation_exports_usage_known_before_failure() -> None:
    exporter = InMemorySpanExporter()
    client = _langfuse(exporter)
    turn = LangfuseTracer(client).start_turn(_info(uuid4().hex))
    generation = turn.generation(
        GenerationInfo(name="llm", model="main", input={}, provider="anthropic")
    )

    generation.fail("обрыв", GenerationUsage(10, 1, cache_read_tokens=4))
    turn.finish({"answer": ""})
    client.flush()

    llm = _attrs(next(s for s in exporter.get_finished_spans() if s.name == "llm"))
    assert json.loads(str(llm["langfuse.observation.usage_details"])) == {
        "input": 6,
        "cache_read_input_tokens": 4,
        "output": 1,
    }
    assert llm["langfuse.observation.level"] == "ERROR"
    client.shutdown()


def test_error_messages_are_masked_before_export() -> None:
    """status_message SDK не маскирует, а сообщения валидации несут значения (O-3)."""
    exporter = InMemorySpanExporter()
    client = _langfuse(exporter)
    turn = LangfuseTracer(client).start_turn(_info(uuid4().hex))
    phone = "+7 999 111-22-33"

    generation = turn.generation(GenerationInfo(name="llm", model="main", input={}))
    generation.fail(f"ответ модели: позвоните {phone}")
    turn.tool_finished(
        "c1", "create_lead", {}, None, f"$.fields.phone: '{phone}' does not match", 3
    )
    turn.finish({"answer": ""}, error=f"ход упал на {phone}")
    client.flush()

    messages = [
        str(_attrs(s).get("langfuse.observation.status_message", ""))
        for s in exporter.get_finished_spans()
    ]
    assert len([m for m in messages if m]) == 3
    assert all("999" not in m for m in messages)
    client.shutdown()


class _BrokenObservation:
    """Observation, у которого падает любой вызов SDK."""

    def __getattr__(self, name: str) -> Any:
        def broken(*_: Any, **__: Any) -> Any:
            raise RuntimeError(f"SDK: {name}")

        return broken


async def test_sdk_failures_mid_turn_do_not_break_the_turn(
    monkeypatch: pytest.MonkeyPatch, debug_logs: list[Any]
) -> None:
    """O-5: трейс создан, дальше SDK падает на каждом вызове — ход и модель отрабатывают."""
    exporter = InMemorySpanExporter()
    client = _langfuse(exporter)
    turn = LangfuseTracer(client).start_turn(_info(uuid4().hex))
    root = turn._root  # type: ignore[attr-defined]
    monkeypatch.setattr(root, "start_observation", _BrokenObservation().start_observation)
    monkeypatch.setattr(root, "update", _BrokenObservation().update)
    monkeypatch.setattr(root, "end", _BrokenObservation().end)

    llm = TracedLLM(FakeLLM([FakeReply("Да")]), turn, "openai")
    chunks = [
        c
        async for c in llm.stream(LLMRequest(model="main", system="", messages=(UserMessage("?"),)))
    ]
    turn.tool_started("c1", "search_catalog")
    turn.tool_finished("c1", "search_catalog", {}, {}, "ошибка", 1)
    turn.finish({"answer": "Да"}, error="сбой")

    assert any(getattr(c, "text", None) == "Да" for c in chunks)
    operations = {log["operation"] for log in debug_logs if log["event"] == "langfuse_trace_failed"}
    assert operations >= {"generation", "tool_started", "tool_finished", "finish"}
    client.shutdown()


def test_failed_update_still_ends_observations(monkeypatch: pytest.MonkeyPatch) -> None:
    """Упал update — span всё равно закрыт и ушёл в экспорт; корень хода — тоже."""
    exporter = InMemorySpanExporter()
    client = _langfuse(exporter)
    turn = LangfuseTracer(client).start_turn(_info(uuid4().hex))
    generation = turn.generation(GenerationInfo(name="llm", model="main", input={}))
    turn.tool_started("c1", "search_catalog")
    turn.tool_started("c2", "get_entity")  # останется прерванным
    for observation in (
        generation._observation,  # type: ignore[attr-defined]
        turn._tools["c1"],  # type: ignore[attr-defined]
        turn._tools["c2"],  # type: ignore[attr-defined]
        turn._root,  # type: ignore[attr-defined]
    ):
        monkeypatch.setattr(observation, "update", _BrokenObservation().update)

    generation.first_chunk()
    generation.finish({"text": "Да"}, GenerationUsage(1, 1))
    turn.tool_finished("c1", "search_catalog", {}, {}, None, 1)
    turn.finish({"answer": "Да"}, error="сбой")
    client.flush()

    assert {s.name for s in exporter.get_finished_spans()} == {
        "turn",
        "llm",
        "search_catalog",
        "get_entity",
    }
    client.shutdown()


async def test_sdk_failures_on_real_observations_are_logged_by_operation(
    monkeypatch: pytest.MonkeyPatch, debug_logs: list[Any]
) -> None:
    exporter = InMemorySpanExporter()
    client = _langfuse(exporter)
    turn = LangfuseTracer(client).start_turn(_info(uuid4().hex))
    generation = turn.generation(GenerationInfo(name="llm", model="main", input={}))
    turn.tool_started("c1", "search_catalog")
    broken = _BrokenObservation()
    for observation in (generation._observation, turn._tools["c1"]):  # type: ignore[attr-defined]
        monkeypatch.setattr(observation, "update", broken.update)
        monkeypatch.setattr(observation, "end", broken.end)

    generation.first_chunk()
    generation.fail("ошибка модели")
    turn.finish({"answer": ""})

    failed = [log for log in debug_logs if log["event"] == "langfuse_trace_failed"]
    assert {log["operation"] for log in failed} == {
        "generation.first_chunk",
        "generation.fail",
        "generation.end",
        "tool_interrupted",
        "tool.end",
    }
    # Текст исключения SDK в лог не попадает (мог бы нести данные observation).
    assert all(log["error_type"] == "RuntimeError" and "exc_info" not in log for log in failed)
    client.shutdown()


async def test_guard_lets_cancellation_through() -> None:
    with pytest.raises(asyncio.CancelledError), _guard("x"):
        raise asyncio.CancelledError
