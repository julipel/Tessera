"""Агентный цикл на FakeLLM: спецификация P2-02, реализация — P2-03."""

import asyncio
import json
from collections.abc import Callable, Sequence
from typing import Any
from uuid import uuid4

import pytest

from app.modules.agent.public import (
    AgentEvent,
    AgentLoop,
    AnswerDelta,
    AssistantMessage,
    ComponentEmitted,
    ConfirmationReply,
    DialogStateUpdated,
    FakeLLM,
    FakeReply,
    FinishReason,
    LLMRequest,
    SuggestionsOffered,
    ToolCall,
    ToolExecutor,
    ToolFinished,
    ToolResultMessage,
    ToolSchema,
    ToolStarted,
    TurnCompleted,
    TurnContext,
    TurnLimits,
    Usage,
    UserMessage,
)
from app.modules.memory.public import DialogState, PendingConfirmation
from app.modules.shared.kernel import TenantId
from app.modules.tools.public import ToolError, ToolErrorCode, ToolResult

FALLBACK = "Извините, сейчас не получается ответить. Попробуйте ещё раз."
HISTORY = (UserMessage("Подбери крем для сухой кожи"),)
SCHEMAS = (
    ToolSchema(
        name="search_catalog",
        description="Поиск по каталогу",
        parameters={"type": "object", "properties": {"query": {"type": "string"}}},
    ),
)
# Подпись есть только у search_catalog и create_lead — у остальных инструментов её нет.
LABELS = {"search_catalog": "Ищу в каталоге", "create_lead": "Оформляю заявку"}
CARD = {"type": "product_card", "entity_id": "e_1"}


def call(call_id: str, args: dict[str, Any] | None = None, *, raw: str | None = None) -> ToolCall:
    arguments = {"query": "крем"} if args is None and raw is None else args
    return ToolCall(
        id=call_id,
        name="search_catalog",
        arguments=arguments,
        raw_arguments=raw if raw is not None else json.dumps(arguments, ensure_ascii=False),
    )


def failure(code: ToolErrorCode, message: str) -> ToolResult:
    return ToolResult(error=ToolError(code=code, message=message, retryable=False))


class FakeTools:
    """ToolExecutor со сценарием по `tool_call_id`; по умолчанию — успешный результат.

    `hang=True` — исполнение висит до отмены; `cancelled` фиксирует, что отмена дошла;
    `delay_s` — длительность пакета."""

    def __init__(
        self,
        results: dict[str, ToolResult] | None = None,
        *,
        hang: bool = False,
        delay_s: float = 0.0,
    ):
        self.results = results or {}
        self.hang = hang
        self.delay_s = delay_s
        self.batches: list[tuple[ToolCall, ...]] = []
        self.states: list[DialogState] = []
        self.confirmed: list[ToolCall] = []
        self.cancelled = False

    def schemas(self) -> tuple[ToolSchema, ...]:
        return SCHEMAS

    def display_label(self, name: str) -> str | None:
        return LABELS.get(name)

    async def execute_many(self, calls: Sequence[ToolCall], ctx: TurnContext) -> list[ToolResult]:
        self.batches.append(tuple(calls))
        self.states.append(ctx.state)
        if self.hang:
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                self.cancelled = True
                raise
        await asyncio.sleep(self.delay_s)
        return [self.results.get(c.id, ToolResult(content={"found": 1})) for c in calls]

    async def execute_confirmed(self, call: ToolCall, ctx: TurnContext) -> ToolResult:
        self.confirmed.append(call)
        return self.results.get(call.id, ToolResult(content="Заявка создана."))

    @property
    def executed(self) -> list[ToolCall]:
        return [c for batch in self.batches for c in batch]


def context(
    *,
    max_steps: int = 6,
    max_tool_retries: int = 2,
    state: DialogState | None = None,
    confirmation: ConfirmationReply | None = None,
) -> TurnContext:
    return TurnContext(
        tenant_id=TenantId(uuid4()),
        conversation_id=uuid4(),
        turn_id=uuid4(),
        model="fake-model",
        system="Ты — консультант.",
        history=HISTORY,
        limits=TurnLimits(max_steps=max_steps, max_tool_retries=max_tool_retries),
        fallback_message=FALLBACK,
        temperature=0.3,
        state=state or DialogState(),
        confirmation=confirmation,
    )


async def run(
    llm: FakeLLM, tools: ToolExecutor, ctx: TurnContext | None = None
) -> list[AgentEvent]:
    return [e async for e in AgentLoop(llm, tools).run_turn(ctx or context())]


def text(events: list[AgentEvent]) -> str:
    return "".join(e.text for e in events if isinstance(e, AnswerDelta))


def completed(events: list[AgentEvent]) -> TurnCompleted:
    assert sum(isinstance(e, TurnCompleted) for e in events) == 1
    last = events[-1]
    assert isinstance(last, TurnCompleted)
    return last


def tool_results(request: LLMRequest) -> list[ToolResultMessage]:
    return [m for m in request.messages if isinstance(m, ToolResultMessage)]


async def test_direct_answer_streams_text_without_tools() -> None:
    llm = FakeLLM([FakeReply(text="Какой у вас бюджет?", usage=Usage(100, 5))])
    tools = FakeTools()

    events = await run(llm, tools)

    assert events[:-1] == [AnswerDelta("Какой "), AnswerDelta("у "), AnswerDelta("вас "),
                           AnswerDelta("бюджет?")]  # fmt: skip
    assert completed(events) == TurnCompleted(FinishReason.ANSWERED, Usage(100, 5), steps=1)
    assert llm.requests == [
        LLMRequest(
            model="fake-model",
            system="Ты — консультант.",
            messages=HISTORY,
            tools=SCHEMAS,
            temperature=0.3,
        )
    ]
    assert tools.batches == []


async def test_single_tool_call_then_answer() -> None:
    search = call("call_1")
    llm = FakeLLM([FakeReply(text="Ищу. ", tool_calls=(search,)), FakeReply(text="Нашла крем.")])
    tools = FakeTools({"call_1": ToolResult(content={"items": ["e_1"]}, components=(CARD,))})

    events = await run(llm, tools)

    assert events[:-1] == [
        AnswerDelta("Ищу. "),
        ToolStarted("call_1", "search_catalog", "Ищу в каталоге"),
        ToolFinished(
            tool_call_id="call_1",
            name="search_catalog",
            ok=True,
            arguments={"query": "крем"},
            content={"items": ["e_1"]},
        ),
        ComponentEmitted(CARD),
        AnswerDelta("Нашла "),
        AnswerDelta("крем."),
    ]
    assert completed(events).finish is FinishReason.ANSWERED
    assert tools.batches == [(search,)]

    second = llm.requests[1]
    assert second.messages[: len(HISTORY) + 1] == (
        *HISTORY,
        AssistantMessage(text="Ищу. ", tool_calls=(search,)),
    )
    [result] = tool_results(second)
    assert result.tool_call_id == "call_1"
    assert result.is_error is False
    assert json.loads(result.content) == {"items": ["e_1"]}


REASONING = {"type": "reasoning", "id": "rs_1", "encrypted_content": "gAAA…", "summary": []}


async def test_provider_items_return_unchanged_in_next_step_of_turn() -> None:
    """ADR-0010: данные провайдера шага (reasoning items) уходят во вход следующего шага
    вместе с вызовами инструментов — без изменений и в том же порядке."""
    search = call("call_1")
    items = (REASONING, {"type": "opaque", "nested": {"keep": [1, 2]}})
    llm = FakeLLM(
        [
            FakeReply(tool_calls=(search,), provider_items=items),
            FakeReply(tool_calls=(call("call_2"),), provider_items=()),
            FakeReply(text="Готово."),
        ]
    )

    await run(llm, FakeTools())

    assert llm.requests[0].messages == HISTORY
    assert llm.requests[1].messages[len(HISTORY)] == AssistantMessage(
        text="", tool_calls=(search,), provider_items=items
    )
    third = [m for m in llm.requests[2].messages if isinstance(m, AssistantMessage)]
    assert [m.provider_items for m in third] == [items, ()]


async def test_tool_chain_accumulates_messages_and_usage() -> None:
    first, second = call("call_1", {"query": "крем"}), call("call_2", {"query": "сыворотка"})
    llm = FakeLLM(
        [
            FakeReply(tool_calls=(first,), usage=Usage(100, 10)),
            FakeReply(tool_calls=(second,), usage=Usage(200, 10)),
            FakeReply(text="Подойдут два крема.", usage=Usage(300, 20)),
        ]
    )
    tools = FakeTools()

    events = await run(llm, tools)

    assert completed(events) == TurnCompleted(FinishReason.ANSWERED, Usage(600, 40), steps=3)
    assert tools.batches == [(first,), (second,)]
    assert [m.tool_call_id for m in tool_results(llm.requests[2])] == ["call_1", "call_2"]
    assert text(events) == "Подойдут два крема."


async def test_parallel_calls_run_in_one_batch_in_call_order() -> None:
    a, b = call("call_a", {"query": "крем"}), call("call_b", {"query": "тоник"})
    llm = FakeLLM([FakeReply(tool_calls=(a, b)), FakeReply(text="Вот два варианта.")])
    tools = FakeTools({"call_b": ToolResult(content="тоник: 2 шт.")})

    events = await run(llm, tools)

    assert tools.batches == [(a, b)]
    tool_events = [e for e in events if isinstance(e, ToolStarted | ToolFinished)]
    assert [type(e) for e in tool_events] == [ToolStarted, ToolStarted, ToolFinished, ToolFinished]
    assert [e.tool_call_id for e in tool_events] == ["call_a", "call_b", "call_a", "call_b"]
    results = tool_results(llm.requests[1])
    assert [m.tool_call_id for m in results] == ["call_a", "call_b"]
    assert results[1].content == "тоник: 2 шт."


async def test_parallel_calls_get_batch_duration() -> None:
    a, b = call("call_a"), call("call_b")
    llm = FakeLLM([FakeReply(tool_calls=(a, b)), FakeReply(text="Готово.")])

    events = await run(llm, FakeTools(delay_s=0.05))

    durations = [e.duration_ms for e in events if isinstance(e, ToolFinished)]
    assert len(durations) == 2 and durations[0] == durations[1] >= 45


async def test_validation_error_goes_to_model_which_corrects_itself() -> None:
    bad, good = call("call_1", {"qeury": "крем"}), call("call_2", {"query": "крем"})
    llm = FakeLLM([FakeReply(tool_calls=(bad,)), FakeReply(tool_calls=(good,)), FakeReply("Ок.")])
    tools = FakeTools({"call_1": failure("validation_error", "query: обязательное поле")})

    events = await run(llm, tools, context(max_tool_retries=1))

    assert (
        ToolFinished(
            "call_1",
            "search_catalog",
            ok=False,
            error_code="validation_error",
            arguments={"qeury": "крем"},
            error_message="query: обязательное поле",
        )
        in events
    )
    [error] = tool_results(llm.requests[1])
    assert error.is_error is True
    assert "query: обязательное поле" in error.content
    assert tools.executed == [bad, good]
    assert completed(events).finish is FinishReason.ANSWERED


async def test_unparseable_arguments_are_rejected_without_executor() -> None:
    broken = call("call_1", raw="{query:")
    assert broken.arguments is None
    llm = FakeLLM([FakeReply(tool_calls=(broken,)), FakeReply(text="Уточните запрос.")])
    tools = FakeTools()

    events = await run(llm, tools)

    assert broken not in tools.executed
    [finished] = [e for e in events if isinstance(e, ToolFinished)]
    assert (finished.ok, finished.error_code, finished.arguments) == (
        False,
        "validation_error",
        "{query:",  # исходная строка: для записи ToolCall сохраняется как прислала модель
    )
    assert finished.error_message is not None and finished.duration_ms == 0
    [error] = tool_results(llm.requests[1])
    assert error.tool_call_id == "call_1"
    assert error.is_error is True
    assert completed(events).finish is FinishReason.ANSWERED


async def test_tool_retries_exhausted_ends_with_fallback() -> None:
    bad = [call(f"call_{i}", {"qeury": "крем"}) for i in range(3)]
    llm = FakeLLM([FakeReply(tool_calls=(c,)) for c in bad])
    tools = FakeTools({c.id: failure("validation_error", "query: обязательное поле") for c in bad})

    events = await run(llm, tools, context(max_tool_retries=2))

    assert len(llm.requests) == 3
    assert llm.remaining == 0
    assert events[-2] == AnswerDelta(FALLBACK)
    assert completed(events).finish is FinishReason.TOOL_RETRIES_EXHAUSTED
    assert completed(events).steps == 3


async def test_step_limit_ends_with_fallback() -> None:
    llm = FakeLLM([FakeReply(text="Ищу. ", tool_calls=(call(f"call_{i}"),)) for i in range(3)])
    tools = FakeTools()

    events = await run(llm, tools, context(max_steps=3))

    assert len(llm.requests) == 3
    assert events[-2] == AnswerDelta(FALLBACK)
    assert completed(events).finish is FinishReason.STEP_LIMIT
    assert completed(events).steps == 3


async def test_tool_timeout_is_reported_to_model_and_not_counted_as_retry() -> None:
    search = call("call_1")
    llm = FakeLLM([FakeReply(tool_calls=(search,)), FakeReply(text="Каталог недоступен.")])
    tools = FakeTools({"call_1": failure("timeout", "search_catalog: нет ответа за 10 с")})

    events = await run(llm, tools, context(max_tool_retries=0))

    [finished] = [e for e in events if isinstance(e, ToolFinished)]
    assert (finished.ok, finished.error_code) == (False, "timeout")
    assert finished.error_message == "search_catalog: нет ответа за 10 с"
    [error] = tool_results(llm.requests[1])
    assert error.is_error is True
    assert "нет ответа" in error.content
    assert completed(events).finish is FinishReason.ANSWERED
    assert text(events) == "Каталог недоступен."


async def wait_until(condition: Callable[[], bool], task: asyncio.Task[Any]) -> None:
    """Ждать условия, пока ход идёт; если ход завершился раньше — поднять исключение хода."""
    for _ in range(200):
        if task.done():
            task.result()
            pytest.fail("ход завершился раньше, чем ожидалось")
        if condition():
            return
        await asyncio.sleep(0.005)
    pytest.fail("условие не наступило")


async def cancel(task: asyncio.Task[Any]) -> None:
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


async def test_cancel_during_tool_execution_cancels_tools() -> None:
    llm = FakeLLM([FakeReply(tool_calls=(call("call_1"),)), FakeReply(text="не дойдёт")])
    tools = FakeTools(hang=True)
    task = asyncio.create_task(run(llm, tools))

    await wait_until(lambda: bool(tools.batches), task)
    await cancel(task)

    assert tools.cancelled is True
    assert len(llm.requests) == 1


async def test_cancel_during_llm_stream_stops_turn() -> None:
    llm = FakeLLM([FakeReply(text="очень долгий ответ", tool_calls=(call("c"),), delay_s=10)])
    tools = FakeTools()
    task = asyncio.create_task(run(llm, tools))

    await wait_until(lambda: bool(llm.requests), task)
    await cancel(task)

    assert tools.batches == []
    assert len(llm.requests) == 1


async def test_state_patches_are_applied_in_call_order() -> None:
    a, b, bad = call("call_a"), call("call_b"), call("call_bad")
    llm = FakeLLM([FakeReply(tool_calls=(a, b, bad)), FakeReply(text="Учла.")])
    tools = FakeTools(
        {
            "call_a": ToolResult(state_patch={"slots": {"budget": 3000, "skin": "сухая"}}),
            "call_b": ToolResult(state_patch={"slots": {"skin": None}, "facts": ["аллергия"]}),
            # Патч из неуспешного вызова не применяется.
            "call_bad": ToolResult(
                state_patch={"slots": {"budget": 1}},
                error=ToolError(code="upstream_error", message="сбой", retryable=False),
            ),
        }
    )
    start = DialogState(slots={"gift": {"recipient": "мама"}}, active_scenario="gift")

    events = await run(llm, tools, context(state=start))

    [updated] = [e for e in events if isinstance(e, DialogStateUpdated)]
    assert updated.state == DialogState(
        slots={"gift": {"recipient": "мама", "budget": 3000}},
        facts=("аллергия",),
        active_scenario="gift",
    )
    assert events.index(updated) < events.index(AnswerDelta("Учла."))


async def test_tools_get_state_of_current_step() -> None:
    # Сценарий, сменённый на шаге 1, действует для слотов шага 2 (ADR-0026).
    llm = FakeLLM(
        [
            FakeReply(tool_calls=(call("call_switch"),)),
            FakeReply(tool_calls=(call("call_slots"),)),
            FakeReply(text="Подберу подарок."),
        ]
    )
    tools = FakeTools({"call_switch": ToolResult(state_patch={"active_scenario": "gift"})})
    start = DialogState(active_scenario="skincare")

    await run(llm, tools, context(state=start))

    assert [s.active_scenario for s in tools.states] == ["skincare", "gift"]


async def test_no_state_event_without_patches() -> None:
    llm = FakeLLM([FakeReply(tool_calls=(call("call_1"),)), FakeReply(text="Готово.")])

    events = await run(llm, FakeTools())

    assert not any(isinstance(e, DialogStateUpdated) for e in events)


async def test_suggestions_from_successful_tool_results() -> None:
    a, bad = call("call_a"), call("call_bad")
    llm = FakeLLM([FakeReply(text="Какой у вас тип кожи?", tool_calls=(a, bad)), FakeReply()])
    tools = FakeTools(
        {
            "call_a": ToolResult(suggestions=("Сухая", "Жирная")),
            # Подсказки из неуспешного вызова не показываются.
            "call_bad": ToolResult(
                suggestions=("Не то",),
                error=ToolError(code="upstream_error", message="сбой", retryable=False),
            ),
        }
    )

    events = await run(llm, tools)

    assert [e for e in events if isinstance(e, SuggestionsOffered)] == [
        SuggestionsOffered(("Сухая", "Жирная"))
    ]
    assert text(events) == "Какой у вас тип кожи?"
    assert completed(events).finish is FinishReason.ANSWERED


PENDING = PendingConfirmation("cf_1", "create_lead", {"form_key": "consultation", "fields": {}})
WAITING = DialogState(facts=("спешит",), pending_confirmation=PENDING)


def last_user_text(request: LLMRequest) -> str:
    last = request.messages[-1]
    assert isinstance(last, UserMessage)
    return last.text


async def test_approved_confirmation_runs_pending_call_before_model() -> None:
    llm = FakeLLM([FakeReply(text="Заявка принята.")])
    tools = FakeTools()

    events = await run(
        llm, tools, context(state=WAITING, confirmation=ConfirmationReply("cf_1", approved=True))
    )

    # Исполнен ожидающий вызов с аргументами из состояния, а не от модели.
    [executed] = tools.confirmed
    assert (executed.id, executed.name, executed.arguments) == (
        "cf_1",
        "create_lead",
        {"form_key": "consultation", "fields": {}},
    )
    assert tools.batches == []
    assert events[0] == ToolStarted("cf_1", "create_lead", "Оформляю заявку")
    finished = next(e for e in events if isinstance(e, ToolFinished))
    assert (finished.ok, finished.content) == (True, "Заявка создана.")
    [updated] = [e for e in events if isinstance(e, DialogStateUpdated)]
    assert updated.state == DialogState(facts=("спешит",))
    note = last_user_text(llm.requests[0])
    assert note.startswith(HISTORY[0].text)
    assert "[Подтверждено, create_lead: Заявка создана.]" in note
    assert text(events) == "Заявка принята."


async def test_cancelled_confirmation_clears_pending_without_running() -> None:
    llm = FakeLLM([FakeReply(text="Хорошо, не отправляю.")])
    tools = FakeTools()

    events = await run(
        llm, tools, context(state=WAITING, confirmation=ConfirmationReply("cf_1", approved=False))
    )

    assert tools.confirmed == []
    assert not any(isinstance(e, ToolStarted | ToolFinished) for e in events)
    [updated] = [e for e in events if isinstance(e, DialogStateUpdated)]
    assert updated.state.pending_confirmation is None
    assert "[Пользователь отменил create_lead" in last_user_text(llm.requests[0])


@pytest.mark.parametrize(
    "state", [DialogState(), WAITING], ids=["nothing_pending", "other_confirm_id"]
)
async def test_stale_confirmation_runs_nothing(state: DialogState) -> None:
    llm = FakeLLM([FakeReply(text="Это подтверждение уже неактуально.")])
    tools = FakeTools()
    reply = ConfirmationReply("cf_old", approved=True)

    events = await run(llm, tools, context(state=state, confirmation=reply))

    assert tools.confirmed == []
    assert not any(isinstance(e, DialogStateUpdated) for e in events)
    assert "[Подтверждение устарело" in last_user_text(llm.requests[0])


async def test_failed_confirmed_call_is_reported_to_model() -> None:
    llm = FakeLLM([FakeReply(text="Не получилось, попробуйте позже.")])
    tools = FakeTools({"cf_1": failure("upstream_error", "create_lead: данные недоступны")})

    events = await run(
        llm, tools, context(state=WAITING, confirmation=ConfirmationReply("cf_1", approved=True))
    )

    finished = next(e for e in events if isinstance(e, ToolFinished))
    assert finished.error_code == "upstream_error"
    # Ожидание снято и при ошибке: повтор — новым вызовом и новым подтверждением.
    [updated] = [e for e in events if isinstance(e, DialogStateUpdated)]
    assert updated.state.pending_confirmation is None
    assert "upstream_error: create_lead: данные недоступны" in last_user_text(llm.requests[0])


# --- Пустой ответ модели (P7-01, architecture.md §12) ---


async def test_empty_response_is_retried_once() -> None:
    llm = FakeLLM([FakeReply(), FakeReply(text="Подойдёт крем с керамидами.")])

    events = await run(llm, FakeTools())

    assert text(events) == "Подойдёт крем с керамидами."
    assert completed(events).finish is FinishReason.ANSWERED
    assert completed(events).steps == 2
    assert llm.requests[0] == llm.requests[1]  # повтор того же шага


async def test_second_empty_response_gives_fallback_message() -> None:
    llm = FakeLLM([FakeReply(chunks=(" ",)), FakeReply()])

    events = await run(llm, FakeTools())

    assert text(events).endswith(context().fallback_message)
    assert completed(events).finish is FinishReason.EMPTY_RESPONSE
    assert llm.remaining == 0


@pytest.mark.parametrize(
    "result",
    [ToolResult(content="ok"), ToolResult(content="ok", components=({"type": "product_card"},))],
)
async def test_empty_last_step_after_visible_output_is_answer(result: ToolResult) -> None:
    # Текст первого шага или показанный компонент — уже ответ: пустой последний шаг не повтор.
    first = FakeReply(
        text="Какой у вас тип кожи?" if not result.components else "", tool_calls=(call("c"),)
    )
    llm = FakeLLM([first, FakeReply()])

    events = await run(llm, FakeTools({"c": result}))

    assert completed(events).finish is FinishReason.ANSWERED
    assert llm.remaining == 0
