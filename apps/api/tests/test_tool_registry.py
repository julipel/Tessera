"""Tool Registry (P2-04): валидация аргументов, параллельность, таймауты, ошибки — в результате."""

import asyncio
import json
from collections.abc import Mapping
from dataclasses import replace
from typing import Any
from uuid import uuid4

import pytest

from app.modules.agent.public import (
    AgentLoop,
    ComponentEmitted,
    ConfirmationReply,
    DialogStateUpdated,
    FakeLLM,
    FakeReply,
    FinishReason,
    RegistryToolExecutor,
    ToolCall,
    ToolFinished,
    ToolResultMessage,
    ToolSchema,
    TurnCompleted,
    TurnContext,
    TurnLimits,
    UserMessage,
)
from app.modules.memory.public import DialogState
from app.modules.shared.kernel import TenantId
from app.modules.tools.public import (
    ConfirmLabels,
    InvalidToolDefinitionError,
    ToolContext,
    ToolDefinition,
    ToolHandler,
    ToolInvocation,
    ToolRegistry,
    ToolResult,
)

CTX = ToolContext(tenant_id=TenantId(uuid4()), conversation_id=uuid4(), turn_id=uuid4())
AVAILABILITY = {
    "type": "object",
    "properties": {"entity_id": {"type": "string"}, "city": {"type": "string"}},
    "required": ["entity_id", "city"],
    "additionalProperties": False,
}
CARD = {"type": "product_card", "entity_id": "e_1"}


async def ok(arguments: Mapping[str, Any], ctx: ToolContext) -> ToolResult:
    return ToolResult(content={"args": dict(arguments)})


def tool(
    name: str = "check_availability",
    handler: ToolHandler = ok,
    *,
    parameters: dict[str, Any] | None = None,
    timeout_s: float = 1,
) -> ToolDefinition:
    return ToolDefinition(
        name=name,
        description="Проверить наличие",
        parameters=AVAILABILITY if parameters is None else parameters,
        handler=handler,
        timeout_s=timeout_s,
    )


def inv(call_id: str, name: str = "check_availability", **arguments: Any) -> ToolInvocation:
    return ToolInvocation(id=call_id, name=name, arguments=arguments)


VALID = {"entity_id": "e_1", "city": "Москва"}


async def test_valid_call_runs_handler_with_context() -> None:
    seen: list[ToolContext] = []

    async def handler(arguments: Mapping[str, Any], ctx: ToolContext) -> ToolResult:
        seen.append(ctx)
        return ToolResult(content="в наличии")

    [result] = await ToolRegistry([tool(handler=handler)]).execute_many([inv("c1", **VALID)], CTX)

    assert result == ToolResult(content="в наличии")
    assert seen == [CTX]


async def test_components_and_state_patch_pass_through() -> None:
    async def handler(arguments: Mapping[str, Any], ctx: ToolContext) -> ToolResult:
        return ToolResult(content="ok", components=(CARD,), state_patch={"city": "Москва"})

    [result] = await ToolRegistry([tool(handler=handler)]).execute_many([inv("c1", **VALID)], CTX)

    assert result.components == (CARD,)
    assert result.state_patch == {"city": "Москва"}


async def test_calls_run_in_parallel_and_results_keep_order() -> None:
    first_started = asyncio.Event()

    async def slow(arguments: Mapping[str, Any], ctx: ToolContext) -> ToolResult:
        first_started.set()
        await asyncio.sleep(0.05)
        return ToolResult(content="slow")

    async def fast(arguments: Mapping[str, Any], ctx: ToolContext) -> ToolResult:
        await first_started.wait()  # дождётся, только если slow уже идёт параллельно
        return ToolResult(content="fast")

    registry = ToolRegistry([tool("slow", slow), tool("fast", fast)])

    results = await asyncio.wait_for(
        registry.execute_many([inv("c1", "slow", **VALID), inv("c2", "fast", **VALID)], CTX), 1
    )

    assert [r.content for r in results] == ["slow", "fast"]


async def test_unknown_tool_is_validation_error_listing_available() -> None:
    [result] = await ToolRegistry([tool()]).execute_many([inv("c1", "search_catalog")], CTX)

    assert result.error is not None
    assert result.error.code == "validation_error"
    assert "search_catalog" in result.error.message
    assert "check_availability" in result.error.message


@pytest.mark.parametrize(
    ("arguments", "expected"),
    [
        ({"entity_id": "e_1"}, "'city' is a required property"),
        ({"entity_id": 1, "city": "Москва"}, "$.entity_id: 1 is not of type 'string'"),
        ({**VALID, "color": "red"}, "'color' was unexpected"),
    ],
)
async def test_invalid_arguments_are_explained_and_handler_not_called(
    arguments: dict[str, Any], expected: str
) -> None:
    calls: list[Mapping[str, Any]] = []

    async def handler(args: Mapping[str, Any], ctx: ToolContext) -> ToolResult:
        calls.append(args)
        return ToolResult(content="ok")

    [result] = await ToolRegistry([tool(handler=handler)]).execute_many(
        [ToolInvocation(id="c1", name="check_availability", arguments=arguments)], CTX
    )

    assert result.error is not None
    assert result.error.code == "validation_error"
    assert not result.error.retryable
    assert expected in result.error.message
    assert calls == []


async def test_all_validation_errors_reported_at_once() -> None:
    [result] = await ToolRegistry([tool()]).execute_many([inv("c1", entity_id=1)], CTX)

    assert result.error is not None
    assert "required" in result.error.message
    assert "not of type" in result.error.message


async def test_timeout_does_not_break_other_calls() -> None:
    async def hangs(arguments: Mapping[str, Any], ctx: ToolContext) -> ToolResult:
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    registry = ToolRegistry([tool("hangs", hangs, timeout_s=0.05), tool("quick")])

    hung, quick = await registry.execute_many(
        [inv("c1", "hangs", **VALID), inv("c2", "quick", **VALID)], CTX
    )

    assert hung.error is not None
    assert hung.error.code == "timeout"
    assert hung.error.retryable
    assert quick.ok


async def test_handler_exception_is_upstream_error_without_internals() -> None:
    async def broken(arguments: Mapping[str, Any], ctx: ToolContext) -> ToolResult:
        raise RuntimeError("postgres://secret@db: connection refused")

    [result] = await ToolRegistry([tool(handler=broken)]).execute_many([inv("c1", **VALID)], CTX)

    assert result.error is not None
    assert result.error.code == "upstream_error"
    assert "secret" not in result.error.message


async def test_timeout_raised_inside_handler_is_upstream_error() -> None:
    async def own_timeout(arguments: Mapping[str, Any], ctx: ToolContext) -> ToolResult:
        raise TimeoutError("httpx read timeout")

    [result] = await ToolRegistry([tool(handler=own_timeout)]).execute_many(
        [inv("c1", **VALID)], CTX
    )

    assert result.error is not None
    assert result.error.code == "upstream_error"


async def test_cancellation_cancels_all_handlers() -> None:
    started = 0
    cancelled = 0
    both_started = asyncio.Event()

    async def hangs(arguments: Mapping[str, Any], ctx: ToolContext) -> ToolResult:
        nonlocal started, cancelled
        started += 1
        if started == 2:
            both_started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled += 1
            raise
        raise AssertionError("unreachable")

    registry = ToolRegistry([tool(handler=hangs, timeout_s=10)])
    task = asyncio.create_task(registry.execute_many([inv("c1", **VALID), inv("c2", **VALID)], CTX))
    await both_started.wait()

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert cancelled == 2


@pytest.mark.parametrize(
    ("definitions", "message"),
    [
        ([tool(), tool()], "дважды"),
        ([tool("CheckAvailability")], "snake_case"),
        ([tool(parameters={"type": "object", "properties": {"x": {"type": "strng"}}})], "Schema"),
        ([tool(parameters={"type": "string"})], "type: object"),
        ([tool(timeout_s=0)], "timeout_s"),
        ([replace(tool(), requires_confirmation=True)], "confirm_text"),
    ],
)
def test_invalid_configuration_is_rejected(definitions: list[ToolDefinition], message: str) -> None:
    with pytest.raises(InvalidToolDefinitionError, match=message):
        ToolRegistry(definitions)


# --- Агентный цикл поверх реестра ---


def turn_context(
    state: DialogState | None = None, confirmation: ConfirmationReply | None = None
) -> TurnContext:
    return TurnContext(
        tenant_id=CTX.tenant_id,
        conversation_id=CTX.conversation_id,
        turn_id=CTX.turn_id,
        model="fake-model",
        system="Ты — консультант.",
        history=(UserMessage("Есть в наличии в Москве?"),),
        limits=TurnLimits(max_steps=6, max_tool_retries=2),
        fallback_message="Извините.",
        state=state or DialogState(),
        confirmation=confirmation,
    )


def call(call_id: str, arguments: dict[str, Any]) -> ToolCall:
    return ToolCall(
        id=call_id,
        name="check_availability",
        arguments=arguments,
        raw_arguments=json.dumps(arguments, ensure_ascii=False),
    )


async def test_executor_exposes_schemas() -> None:
    executor = RegistryToolExecutor(ToolRegistry([tool()]))

    assert executor.schemas() == (
        ToolSchema(
            name="check_availability", description="Проверить наличие", parameters=AVAILABILITY
        ),
    )


async def test_executor_exposes_display_labels() -> None:
    labelled = replace(tool("search_catalog"), display_label="Ищу в каталоге")
    executor = RegistryToolExecutor(ToolRegistry([tool(), labelled]))

    assert executor.display_label("search_catalog") == "Ищу в каталоге"
    assert executor.display_label("check_availability") is None
    assert executor.display_label("unknown") is None


async def test_agent_recovers_from_validation_error_via_registry() -> None:
    async def handler(arguments: Mapping[str, Any], ctx: ToolContext) -> ToolResult:
        assert ctx == CTX
        return ToolResult(content={"available": True}, components=(CARD,))

    llm = FakeLLM(
        [
            FakeReply(tool_calls=(call("c1", {"entity_id": "e_1"}),)),
            FakeReply(tool_calls=(call("c2", VALID),)),
            FakeReply(text="Да, есть."),
        ]
    )
    loop = AgentLoop(llm, RegistryToolExecutor(ToolRegistry([tool(handler=handler)])))

    events = [e async for e in loop.run_turn(turn_context())]

    finished = [e for e in events if isinstance(e, ToolFinished)]
    assert [(e.ok, e.error_code) for e in finished] == [(False, "validation_error"), (True, None)]
    assert ComponentEmitted(CARD) in events
    last = events[-1]
    assert isinstance(last, TurnCompleted)
    assert (last.finish, last.steps) == (FinishReason.ANSWERED, 3)
    [rejected] = [m for m in llm.requests[1].messages if isinstance(m, ToolResultMessage)]
    assert rejected.is_error
    assert "'city' is a required property" in rejected.content


# --- Подтверждение (ADR-0021) ---


class Recorder:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def __call__(self, arguments: Mapping[str, Any], ctx: ToolContext, /) -> ToolResult:
        self.calls.append(dict(arguments))
        return ToolResult(content="Забронировано.")


def booking(handler: ToolHandler, **overrides: Any) -> ToolDefinition:
    return replace(
        tool("book", handler),
        side_effect=True,
        requires_confirmation=True,
        confirm_text=lambda a: f"Забронировать {a['entity_id']} в городе {a['city']}?",
        **overrides,
    )


async def test_call_requiring_confirmation_returns_confirm_instead_of_running() -> None:
    handler = Recorder()
    registry = ToolRegistry([booking(handler)], ConfirmLabels(confirm="Да", cancel="Нет"))

    [result] = await registry.execute_many([inv("c1", "book", **VALID)], CTX)

    assert handler.calls == []
    [confirm] = result.components
    confirm_id = confirm["confirm_id"]
    assert confirm["type"] == "confirm"
    assert confirm["text"] == "Забронировать e_1 в городе Москва?"
    assert (confirm["confirm_action"]["action_id"], confirm["confirm_action"]["label"]) == (
        "confirm",
        "Да",
    )
    assert (confirm["cancel_action"]["action_id"], confirm["cancel_action"]["label"]) == (
        "cancel",
        "Нет",
    )
    assert confirm["confirm_action"]["payload"] == {"confirm_id": confirm_id}
    assert result.state_patch == {
        "pending_confirmation": {"confirm_id": confirm_id, "tool": "book", "arguments": VALID}
    }
    assert isinstance(result.content, str) and "не вызывай инструмент повторно" in result.content


async def test_invalid_arguments_are_rejected_before_confirmation() -> None:
    def check(arguments: Mapping[str, Any]) -> str | None:
        return "город не обслуживается" if arguments["city"] == "Тверь" else None

    registry = ToolRegistry([booking(Recorder(), check=check)])

    [missing, unserved] = await registry.execute_many(
        [inv("c1", "book", entity_id="e_1"), inv("c2", "book", entity_id="e_1", city="Тверь")],
        CTX,
    )

    assert missing.error is not None and missing.error.code == "validation_error"
    assert unserved.error is not None
    assert unserved.error.code == "validation_error"
    assert "город не обслуживается" in unserved.error.message
    assert missing.components == unserved.components == ()


async def test_confirmed_call_runs_handler_and_is_still_validated() -> None:
    handler = Recorder()
    registry = ToolRegistry([booking(handler)])

    result = await registry.execute_confirmed(inv("cf_1", "book", **VALID), CTX)
    invalid = await registry.execute_confirmed(inv("cf_2", "book", entity_id="e_1"), CTX)

    assert result.content == "Забронировано."
    assert handler.calls == [VALID]
    assert invalid.error is not None and invalid.error.code == "validation_error"


async def test_side_effect_runs_once_only_after_user_confirms() -> None:
    handler = Recorder()
    executor = RegistryToolExecutor(ToolRegistry([booking(handler)]))
    book = replace(call("c1", VALID), name="book")
    llm = FakeLLM(
        [
            FakeReply(text="Проверьте бронь.", tool_calls=(book,)),
            FakeReply(),
            FakeReply(text="Готово, забронировано."),
            FakeReply(text="Бронь уже оформлена."),
        ]
    )

    async def turn(
        state: DialogState | None = None, reply: ConfirmationReply | None = None
    ) -> list[Any]:
        return [e async for e in AgentLoop(llm, executor).run_turn(turn_context(state, reply))]

    first = await turn()
    [confirm] = [e.component for e in first if isinstance(e, ComponentEmitted)]
    [waiting] = [e.state for e in first if isinstance(e, DialogStateUpdated)]
    reply = ConfirmationReply(confirm["confirm_id"], approved=True)
    assert handler.calls == []

    second = await turn(waiting, reply)
    [after] = [e.state for e in second if isinstance(e, DialogStateUpdated)]
    # Повторное нажатие той же кнопки (двойной клик, старое сообщение) — не второй вызов.
    await turn(after, reply)

    assert handler.calls == [VALID]
    assert any(isinstance(e, ToolFinished) and e.ok for e in second)
