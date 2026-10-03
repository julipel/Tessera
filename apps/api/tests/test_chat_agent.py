"""Подключение агента к chat (P2-08): TurnRequest → TurnContext, история для модели, LLMClients."""

from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import pytest

from app.modules.agent.public import (
    AnthropicLLM,
    AssistantMessage,
    FakeLLM,
    FakeReply,
    LLMClients,
    LLMError,
    OpenAILLM,
    RegistryToolExecutor,
    UserMessage,
)
from app.modules.chat.domain.entities import ChatMessage, MessageRole, MessageStatus, TurnRequest
from app.modules.chat.infrastructure.loop_agent import LoopTurnAgent, to_llm_messages
from app.modules.shared.kernel import TenantId
from app.modules.tools.public import ToolRegistry

NOW = datetime(2026, 10, 3, 12, 0, tzinfo=UTC)
TENANT = TenantId(uuid4())


def message(
    role: MessageRole, content: str, user_input: dict[str, Any] | None = None
) -> ChatMessage:
    return ChatMessage(
        id=uuid4(),
        tenant_id=TENANT,
        conversation_id=uuid4(),
        role=role,
        status=MessageStatus.COMPLETED,
        content=content,
        input=user_input,
        blocks=[],
        client_message_id=None,
        created_at=NOW,
    )


def test_history_maps_inputs_and_skips_empty_answers() -> None:
    history = [
        message(MessageRole.USER, "Привет", {"type": "text", "text": "Привет"}),
        message(MessageRole.ASSISTANT, ""),
        message(
            MessageRole.USER, "", {"type": "action", "action_id": "pick", "payload": {"id": 7}}
        ),
        message(MessageRole.ASSISTANT, "Отличный выбор."),
        message(
            MessageRole.USER,
            "",
            {"type": "form_submit", "form_id": "contact", "values": {"name": "Аня"}},
        ),
    ]

    assert list(to_llm_messages(history)) == [
        UserMessage("Привет"),
        UserMessage('[Нажата кнопка pick {"id": 7}]'),
        AssistantMessage("Отличный выбор."),
        UserMessage('[Отправлена форма contact: {"name": "Аня"}]'),
    ]


def config(primary: dict[str, Any], limits: dict[str, Any] | None = None) -> dict[str, Any]:
    return {
        "assistant": {"name": "A", "greeting": "Привет!", "fallback_message": "Не вышло."},
        "model": {"primary": primary},
        "limits": limits or {},
        "prompt": {"tenant": "Ты — консультант."},
        "tools": {},
    }


async def run(agent_config: dict[str, Any], llm: FakeLLM) -> None:
    agent = LoopTurnAgent(lambda _: llm, RegistryToolExecutor(ToolRegistry([])), now=lambda: NOW)
    request = TurnRequest(
        tenant_id=TENANT,
        conversation_id=uuid4(),
        agent_config_id=uuid4(),
        turn_id=uuid4(),
        input={"type": "text", "text": "Привет"},
        agent_config=agent_config,
        history=(message(MessageRole.USER, "Привет", {"type": "text", "text": "Привет"}),),
    )
    async for _ in agent.run_turn(request):
        pass


@pytest.mark.parametrize(
    ("primary", "expected"),
    [
        ({"provider": "openai", "name": "m"}, None),  # не задана — значение провайдера
        ({"provider": "openai", "name": "m", "temperature": 0.7}, 0.7),
    ],
)
async def test_temperature_is_passed_only_when_set_by_tenant(
    primary: dict[str, Any], expected: float | None
) -> None:
    llm = FakeLLM([FakeReply(text="Здравствуйте")])

    await run(config(primary), llm)

    assert llm.requests[0].temperature == expected


async def test_model_and_runtime_context_come_from_config() -> None:
    llm = FakeLLM([FakeReply(text="Здравствуйте")])

    await run(config({"provider": "anthropic", "name": "m"}), llm)

    [request] = llm.requests
    assert request.model == "m"
    assert "Текущие дата и время: 2026-10-03T12:00+00:00" in request.system


def test_llm_clients_without_key_raise_non_retryable_error() -> None:
    clients = LLMClients(openai_api_key="sk-test")

    with pytest.raises(LLMError) as error:
        clients.for_provider("anthropic")

    assert error.value.retryable is False


def test_llm_clients_create_client_once_per_provider() -> None:
    clients = LLMClients(openai_api_key="sk-test", anthropic_api_key="sk-ant-test")

    openai = clients.for_provider("openai")

    assert isinstance(openai, OpenAILLM)
    assert clients.for_provider("openai") is openai
    assert isinstance(clients.for_provider("anthropic"), AnthropicLLM)
