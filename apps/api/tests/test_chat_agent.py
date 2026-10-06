"""Подключение агента к chat (P2-08): TurnRequest → TurnContext, история для модели, LLMClients."""

from collections.abc import Mapping
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import pytest

from app.modules.agent.public import (
    AnswerDelta,
    AnthropicLLM,
    AssistantMessage,
    ComponentEmitted,
    ConfirmationReply,
    DialogStateUpdated,
    FakeLLM,
    FakeReply,
    LLMClients,
    LLMError,
    OpenAILLM,
    OpenAIResponsesLLM,
    Provider,
    RegistryToolExecutor,
    ToolCall,
    ToolFinished,
    ToolStarted,
    UserMessage,
)
from app.modules.chat.domain.entities import ChatMessage, MessageRole, MessageStatus, TurnRequest
from app.modules.chat.infrastructure.loop_agent import (
    LoopTurnAgent,
    builtin_turn_agent,
    client_script,
    confirmation_reply,
    to_llm_messages,
)
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
            {
                "type": "action",
                "action_id": "ask_about",
                "label": "Подробнее — Крем",
                "payload": {"entity_id": "e1"},
            },
        ),
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
        # Подпись кнопки — то, что видел пользователь; id и payload — для инструментов.
        UserMessage('[Нажата кнопка «Подробнее — Крем» (ask_about) {"entity_id": "e1"}]'),
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


async def run(
    agent_config: dict[str, Any], llm: FakeLLM, providers: list[Provider] | None = None
) -> None:
    """`providers` — куда записать, для каких провайдеров агент запрашивал клиента."""

    def llm_for(provider: Provider) -> FakeLLM:
        if providers is not None:
            providers.append(provider)
        return llm

    agent = LoopTurnAgent(
        llm_for, lambda *_: RegistryToolExecutor(ToolRegistry([])), now=lambda: NOW
    )
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
    clients = LLMClients(
        openai_api_key="sk-test",
        openai_compatible_api_key="sk-compat",
        anthropic_api_key="sk-ant-test",
    )

    openai = clients.for_provider("openai")

    assert isinstance(openai, OpenAIResponsesLLM)  # ADR-0010: openai — Responses API
    assert clients.for_provider("openai") is openai
    assert isinstance(clients.for_provider("openai_compatible"), OpenAILLM)
    assert isinstance(clients.for_provider("anthropic"), AnthropicLLM)


def test_openai_compatible_needs_own_key() -> None:
    """ADR-0010: ключ `openai` не подходит для `openai_compatible` — у них разные endpoint."""
    clients = LLMClients(openai_api_key="sk-test")

    with pytest.raises(LLMError) as error:
        clients.for_provider("openai_compatible")

    assert error.value.retryable is False


async def test_openai_compatible_provider_selects_its_client() -> None:
    llm = FakeLLM([FakeReply(text="Здравствуйте")])
    providers: list[Provider] = []

    await run(config({"provider": "openai_compatible", "name": "anthropic/m"}), llm, providers)

    assert providers == ["openai_compatible"]
    assert llm.requests[0].model == "anthropic/m"


# --- Подтверждение create_lead через ход chat (P5-04a, ADR-0021) ---

LEAD_CONFIG: dict[str, Any] = {
    **config({"provider": "openai", "name": "m"}),
    "assistant": {
        "name": "A",
        "greeting": "Привет!",
        "fallback_message": "Не вышло.",
        "confirm_labels": {"confirm": "Да, отправить"},
    },
    "tools": {"builtin": ["create_lead"]},
    "forms": {
        "contact": {
            "title": "Контакт",
            "fields": [{"name": "phone", "label": "Телефон", "kind": "text", "required": True}],
        }
    },
}


class RecordingLeads:
    def __init__(self) -> None:
        self.fields: list[dict[str, str]] = []

    async def create(
        self, tenant_id: TenantId, conversation_id: UUID, form_key: str, fields: Mapping[str, str]
    ) -> UUID:
        self.fields.append(dict(fields))
        return uuid4()


def lead_request(user_input: dict[str, Any], state: dict[str, Any]) -> TurnRequest:
    return TurnRequest(
        tenant_id=TENANT,
        conversation_id=uuid4(),
        agent_config_id=uuid4(),
        turn_id=uuid4(),
        input=user_input,
        agent_config=LEAD_CONFIG,
        history=(message(MessageRole.USER, "", user_input),),
        dialog_state=state,
    )


async def test_lead_is_created_only_after_confirm_button() -> None:
    lead_call = ToolCall(
        id="c1",
        name="create_lead",
        arguments={"form_key": "contact", "fields": {"phone": "+7900"}},
        raw_arguments="{}",
    )
    llm = FakeLLM(
        [
            FakeReply(text="Проверьте заявку.", tool_calls=(lead_call,)),
            FakeReply(),
            FakeReply(text="Заявка отправлена."),
        ]
    )
    leads = RecordingLeads()
    agent = builtin_turn_agent(lambda _: llm, leads=leads)

    first = [e async for e in agent.run_turn(lead_request({"type": "text", "text": "Да"}, {}))]
    [confirm] = [e.component for e in first if isinstance(e, ComponentEmitted)]
    [waiting] = [e.state for e in first if isinstance(e, DialogStateUpdated)]
    assert leads.fields == []
    assert confirm["confirm_action"]["label"] == "Да, отправить"
    assert confirm["cancel_action"]["label"] == "Отмена"  # по умолчанию из схемы

    press = {
        "type": "action",
        "action_id": "confirm",
        "label": "Да, отправить",
        "payload": {"confirm_id": confirm["confirm_id"]},
    }
    second = [e async for e in agent.run_turn(lead_request(press, waiting.to_dict()))]

    assert leads.fields == [{"phone": "+7900"}]
    assert any(isinstance(e, ToolFinished) and e.ok for e in second)
    # Ожидающий вызов — не знание о пользователе: в Runtime-слой промпта не попадает.
    assert "pending_confirmation" not in llm.requests[-1].system


async def test_english_conversation_gets_translated_and_platform_labels() -> None:
    """Язык диалога en (ADR-0025): подпись тенанта — из translations, без перевода —
    подпись платформы на en, статус инструмента — на en."""
    lead_call = ToolCall(
        id="c1",
        name="create_lead",
        arguments={"form_key": "contact", "fields": {"phone": "+7900"}},
        raw_arguments="{}",
    )
    llm = FakeLLM([FakeReply(text="Please check.", tool_calls=(lead_call,)), FakeReply()])
    agent_config = {
        **LEAD_CONFIG,
        "assistant": {
            **LEAD_CONFIG["assistant"],
            "translations": {"en": {"confirm_labels": {"confirm": "Yes, send"}}},
        },
    }
    request = replace(
        lead_request({"type": "text", "text": "Yes"}, {}),
        agent_config=agent_config,
        language="en",
    )

    events = [
        e async for e in builtin_turn_agent(lambda _: llm, leads=RecordingLeads()).run_turn(request)
    ]

    [confirm] = [e.component for e in events if isinstance(e, ComponentEmitted)]
    assert (confirm["confirm_action"]["label"], confirm["cancel_action"]["label"]) == (
        "Yes, send",
        "Cancel",
    )
    [started] = [e for e in events if isinstance(e, ToolStarted)]
    assert started.display_label == "Preparing your request"
    # В истории нет текста клиента — язык интерфейса остаётся запасным языком ответа.
    assert "Интерфейс клиента — на английском языке" in llm.requests[0].system


@pytest.mark.parametrize(
    ("texts", "expected"),
    [
        (["Нужен крем"], "cyrillic"),
        (["Нужен крем", "ok, the second one"], "latin"),
        (["Hej! Jag söker en parfym"], "latin"),
        (["Hi", ""], "latin"),  # пустое сообщение (кнопка) не в счёт — берётся прошлый текст
        (["42 👍"], None),
        ([], None),
    ],
)
def test_client_script_of_last_text_message(texts: list[str], expected: str | None) -> None:
    history = [message(MessageRole.USER, t, {"type": "text", "text": t}) for t in texts]

    assert client_script(history) == expected


async def test_client_script_hint_in_prompt() -> None:
    llm = FakeLLM([FakeReply(text="Hello!")])
    user_input = {"type": "text", "text": "Hi, I need a cream"}
    request = TurnRequest(
        tenant_id=TENANT,
        conversation_id=uuid4(),
        agent_config_id=uuid4(),
        turn_id=uuid4(),
        input=user_input,
        agent_config=config({"provider": "openai", "name": "m"}),
        history=(message(MessageRole.USER, user_input["text"], user_input),),
        language="ru",
    )

    async for _ in builtin_turn_agent(lambda _: llm).run_turn(request):
        pass

    system = llm.requests[0].system
    assert "Последнее текстовое сообщение клиента написано латиницей" in system
    # Клиент пишет текстом — язык интерфейса (ru) в промпт не идёт.
    assert "Интерфейс клиента" not in system


async def test_interface_language_in_prompt_only_before_client_text() -> None:
    llm = FakeLLM([FakeReply(text="Hello!")])
    press = {"type": "action", "action_id": "start", "label": "Start"}
    request = TurnRequest(
        tenant_id=TENANT,
        conversation_id=uuid4(),
        agent_config_id=uuid4(),
        turn_id=uuid4(),
        input=press,
        agent_config=config({"provider": "openai", "name": "m"}),
        history=(message(MessageRole.USER, "", press),),
        language="en",
    )

    async for _ in builtin_turn_agent(lambda _: llm).run_turn(request):
        pass

    assert "Интерфейс клиента — на английском языке" in llm.requests[0].system


async def test_fallback_message_in_conversation_language() -> None:
    state_call = ToolCall(
        id="c1", name="update_dialog_state", arguments={"facts": ["x"]}, raw_arguments="{}"
    )
    llm = FakeLLM([FakeReply(tool_calls=(state_call,))])
    agent_config = {
        **config({"provider": "openai", "name": "m"}, {"max_steps": 1}),
        "tools": {"builtin": ["update_dialog_state"]},
    }
    agent_config["assistant"] = {
        **agent_config["assistant"],
        "language": "auto",
        "translations": {"sv": {"fallback_message": "Något gick fel."}},
    }
    user_input = {"type": "text", "text": "Hej"}
    request = TurnRequest(
        tenant_id=TENANT,
        conversation_id=uuid4(),
        agent_config_id=uuid4(),
        turn_id=uuid4(),
        input=user_input,
        agent_config=agent_config,
        history=(message(MessageRole.USER, "Hej", user_input),),
        language="sv",
    )

    events = [e async for e in builtin_turn_agent(lambda _: llm).run_turn(request)]

    assert [e.text for e in events if isinstance(e, AnswerDelta)] == ["Något gick fel."]


@pytest.mark.parametrize(
    ("user_input", "expected"),
    [
        (
            {"type": "action", "action_id": "confirm", "payload": {"confirm_id": "cf_1"}},
            ConfirmationReply("cf_1", approved=True),
        ),
        (
            {"type": "action", "action_id": "cancel", "payload": {"confirm_id": "cf_1"}},
            ConfirmationReply("cf_1", approved=False),
        ),
        ({"type": "action", "action_id": "confirm", "payload": {}}, None),
        ({"type": "action", "action_id": "ask_about", "payload": {"confirm_id": "cf_1"}}, None),
        ({"type": "text", "text": "да"}, None),
    ],
)
def test_confirmation_reply_from_input(
    user_input: dict[str, Any], expected: ConfirmationReply | None
) -> None:
    assert confirmation_reply(user_input) == expected


# --- Выбор сценария моделью (P6-01) ---

SCENARIO_CONFIG: dict[str, Any] = {
    **config({"provider": "openai", "name": "m"}),
    "prompt": {
        "tenant": "Ты — консультант.",
        "scenarios": [
            {
                "key": "gift",
                "description": "Подарок",
                "instructions": "Критично: кому и бюджет.",
                "slots": {"recipient": {"type": "string"}},
            },
            {"key": "support", "description": "Доставка и возврат", "instructions": "По базе."},
        ],
    },
    "tools": {"builtin": ["update_dialog_state"]},
}


def scenario_request(text: str, state: dict[str, Any]) -> TurnRequest:
    user_input = {"type": "text", "text": text}
    return TurnRequest(
        tenant_id=TENANT,
        conversation_id=uuid4(),
        agent_config_id=uuid4(),
        turn_id=uuid4(),
        input=user_input,
        agent_config=SCENARIO_CONFIG,
        history=(message(MessageRole.USER, text, user_input),),
        dialog_state=state,
    )


async def test_scenario_chosen_by_model_narrows_next_turn_prompt() -> None:
    choose = ToolCall(
        id="c1",
        name="update_dialog_state",
        arguments={"scenario": "gift", "slots": {"recipient": "мама"}},
        raw_arguments="{}",
    )
    llm = FakeLLM(
        [
            FakeReply(tool_calls=(choose,)),
            FakeReply(text="Какой бюджет?"),
            FakeReply(text="Вот варианты."),
        ]
    )
    agent = builtin_turn_agent(lambda _: llm)

    first = [e async for e in agent.run_turn(scenario_request("Подарок маме", {}))]
    [state] = [e.state for e in first if isinstance(e, DialogStateUpdated)]
    assert state.active_scenario == "gift"
    # Первый ход — все сценарии целиком, модели нужно выбрать.
    assert "## support — Доставка и возврат" in llm.requests[0].system

    [e async for e in agent.run_turn(scenario_request("До 3000", state.to_dict()))]

    system = llm.requests[-1].system
    assert "Активный сценарий: gift." in system
    assert "## gift — Подарок" in system
    assert "## support" not in system
    assert "- support: Доставка и возврат" in system
