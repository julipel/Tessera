"""Сгенерированные контракты (ADR-0005) принимают примеры из docs/contracts.md
и отклоняют невалидные данные."""

import importlib
import inspect
import pkgutil
import re
from typing import Any

import pytest
from pydantic import BaseModel, ValidationError

from app.contracts import (
    ActionInput,
    AgentConfig,
    Component,
    ComponentEvent,
    DoneEvent,
    Event,
    FormSubmitInput,
    HttpToolDefinition,
    TextDeltaData,
    TextDeltaEvent,
    TextInput,
    TurnStartedEvent,
    UserInput,
    generated,
)

ENVELOPE: dict[str, Any] = {
    "protocol_version": "1",
    "seq": 7,
    "conversation_id": "c_1",
    "turn_id": "t_1",
    "message_id": "m_1",
    "ts": "2026-09-28T12:00:00Z",
}

ACTION = {"action_id": "select_product", "label": "Выбрать", "style": "primary"}

PRODUCT_CARD = {
    "type": "product_card",
    "entity_id": "e_1",
    "title": "Крем",
    "price": {"amount": 4990, "currency": "SEK"},
    "badges": ["В наличии"],
    "actions": [ACTION],
}

HTTP_TOOL: dict[str, Any] = {
    "name": "check_availability",
    "kind": "http",
    "description": "Проверить наличие товара в магазине по городу",
    "parameters": {
        "type": "object",
        "properties": {"entity_id": {"type": "string"}, "city": {"type": "string"}},
        "required": ["entity_id", "city"],
    },
    "request": {
        "method": "GET",
        "url": "https://api.example.com/stock/{entity_id}?city={city}",
        "auth": {"type": "bearer", "secret_ref": "EXAMPLE_API_TOKEN"},
    },
    "response": {"content_jmespath": "{available: available, stores: stores[].name}"},
    "timeout_s": 5,
}

AGENT_CONFIG: dict[str, Any] = {
    "assistant": {
        "name": "Ассистент Example",
        "language": "auto",
        "greeting": "Привет! Помогу подобрать...",
        "starter_suggestions": ["Подобрать подарок", "Условия доставки"],
        "fallback_message": "Извините, сейчас не получается ответить.",
    },
    "model": {
        "primary": {"provider": "openai", "name": "<model>", "temperature": 0.3},
        "fallback": {"provider": "anthropic", "name": "<model>"},
    },
    "limits": {
        "max_steps": 6,
        "max_tool_calls_per_step": 4,
        "turn_timeout_s": 60,
        "max_tool_retries": 2,
    },
    "prompt": {
        "tenant": "Ты — консультант компании Example...",
        "scenarios": [
            {
                "key": "product_selection",
                "description": "Подбор товара под задачу пользователя",
                "instructions": "Выясни назначение, бюджет...",
                "slots": {
                    "budget": {"type": "number", "description": "Бюджет"},
                    "recipient": {"type": "string"},
                },
            }
        ],
    },
    "tools": {
        "builtin": ["search_knowledge", "search_catalog", "get_entity"],
        "custom": [HTTP_TOOL],
    },
    "forms": {
        "contact": {
            "title": "Оставьте контакт",
            "fields": [{"name": "contact", "label": "Телефон или Telegram", "kind": "text"}],
        }
    },
    "knowledge": {
        "search_knowledge": {"top_k": 6, "rerank": True},
        "catalog": {"filterable_attributes": ["color", "size"]},
    },
    "branding": {"tokens": {"primary": "#1F4FFF", "radius": "12px", "font": "Inter"}},
}


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        ({"type": "text", "text": "Нужен подарок маме, до 5000"}, TextInput),
        (
            {"type": "action", "action_id": "select_product", "payload": {"entity_id": "e_1"}},
            ActionInput,
        ),
        ({"type": "form_submit", "form_id": "f_1", "values": {"phone": "+46"}}, FormSubmitInput),
    ],
)
def test_user_input_resolves_variant_by_type(payload: dict[str, Any], expected: type) -> None:
    assert isinstance(UserInput.model_validate(payload).root, expected)


def test_user_input_rejects_unknown_type() -> None:
    with pytest.raises(ValidationError):
        UserInput.model_validate({"type": "voice", "text": "..."})


@pytest.mark.parametrize(
    ("event_type", "data", "expected"),
    [
        ("turn_started", {}, TurnStartedEvent),
        ("text_delta", {"block_id": "b1", "delta": "При"}, TextDeltaEvent),
        ("component", {"block_id": "b2", "component": PRODUCT_CARD}, ComponentEvent),
        ("done", {"status": "completed"}, DoneEvent),
    ],
)
def test_event_resolves_variant_by_type(
    event_type: str, data: dict[str, Any], expected: type
) -> None:
    event = Event.model_validate({**ENVELOPE, "type": event_type, "data": data})
    assert isinstance(event.root, expected)


def test_event_json_roundtrip() -> None:
    raw = {**ENVELOPE, "type": "text_delta", "data": {"block_id": "b1", "delta": "x"}}
    event = Event.model_validate(raw)
    assert Event.model_validate_json(event.model_dump_json()) == event


def test_event_built_from_named_data_type() -> None:
    event = TextDeltaEvent(
        **ENVELOPE, type="text_delta", data=TextDeltaData(block_id="b1", delta="x")
    )
    parsed = Event.model_validate_json(event.model_dump_json())
    assert isinstance(parsed.root, TextDeltaEvent)
    assert parsed.root.data == TextDeltaData(block_id="b1", delta="x")


def test_generated_types_have_stable_names() -> None:
    """Вложенные объекты в схемах должны иметь title, иначе генератор называет их
    Data1, Items, Model… и имена сдвигаются при добавлении новых."""
    anonymous = [
        f"{info.name}.{name}"
        for info in pkgutil.iter_modules(generated.__path__)
        for name, obj in inspect.getmembers(
            importlib.import_module(f"{generated.__name__}.{info.name}"), inspect.isclass
        )
        if issubclass(obj, BaseModel) and re.fullmatch(r"(Data|Items?|Usage|Model)\d*", name)
    ]
    assert anonymous == []


def test_event_rejects_extra_data_fields() -> None:
    with pytest.raises(ValidationError):
        Event.model_validate(
            {**ENVELOPE, "type": "text_done", "data": {"block_id": "b1", "raw": "..."}}
        )


@pytest.mark.parametrize(
    "payload",
    [
        PRODUCT_CARD,
        {"type": "product_carousel", "title": "Подходящие варианты", "items": [PRODUCT_CARD]},
        {"type": "info_card", "title": "Доставка", "body_markdown": "...", "image_url": None},
        {
            "type": "comparison_table",
            "columns": ["Модель A", "Модель B"],
            "rows": [{"label": "Цена", "values": ["4990 SEK", "5490 SEK"]}],
        },
        {
            "type": "form",
            "form_id": "f_1",
            "title": "Оставьте контакт",
            "fields": [{"name": "phone", "label": "Телефон", "kind": "phone", "required": True}],
            "submit_label": "Отправить",
        },
        {
            "type": "confirm",
            "confirm_id": "cf_1",
            "text": "Создать заявку?",
            "confirm_action": ACTION,
            "cancel_action": {**ACTION, "action_id": "cancel", "label": "Отмена"},
        },
    ],
)
def test_component_examples_are_valid(payload: dict[str, Any]) -> None:
    assert Component.model_validate(payload).root.type == payload["type"]


def test_component_rejects_unknown_type() -> None:
    with pytest.raises(ValidationError):
        Component.model_validate({"type": "video", "url": "..."})


@pytest.mark.xfail(
    strict=True,
    reason="if/then из JSON Schema (select требует options) datamodel-codegen не переносит",
)
def test_form_select_without_options_rejected() -> None:
    form = {
        "type": "form",
        "form_id": "f_1",
        "title": "Когда удобно?",
        "fields": [{"name": "time", "label": "Время", "kind": "select"}],
    }
    with pytest.raises(ValidationError):
        Component.model_validate(form)


def test_agent_config_example_is_valid() -> None:
    config = AgentConfig.model_validate(AGENT_CONFIG)
    assert config.limits.max_steps == 6
    assert config.tools.custom is not None
    assert isinstance(config.tools.custom[0], HttpToolDefinition)


def test_agent_config_rejects_unknown_field() -> None:
    with pytest.raises(ValidationError):
        AgentConfig.model_validate({**AGENT_CONFIG, "business_logic": True})
