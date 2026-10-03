"""Tool Registry (P2-04): валидация аргументов, параллельность, таймауты, ошибки — в результате."""

import asyncio
import json
from collections.abc import Mapping
from typing import Any
from uuid import uuid4

import pytest

from app.modules.agent.public import (
    AgentLoop,
    ComponentEmitted,
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
from app.modules.shared.kernel import TenantId
from app.modules.tools.public import (
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
    ],
)
def test_invalid_configuration_is_rejected(definitions: list[ToolDefinition], message: str) -> None:
    with pytest.raises(InvalidToolDefinitionError, match=message):
        ToolRegistry(definitions)


# --- Агентный цикл поверх реестра ---


def turn_context() -> TurnContext:
    return TurnContext(
        tenant_id=CTX.tenant_id,
        conversation_id=CTX.conversation_id,
        turn_id=CTX.turn_id,
        model="fake-model",
        system="Ты — консультант.",
        history=(UserMessage("Есть в наличии в Москве?"),),
        limits=TurnLimits(max_steps=6, max_tool_retries=2),
        fallback_message="Извините.",
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
