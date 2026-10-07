"""Маскирование персональных данных в трейсах Langfuse (P7-07, ADR-0032)."""

import json
from typing import Any
from uuid import uuid4

import pytest
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from app.modules.observability.domain.pii import HIDDEN, mask_pii
from app.modules.observability.kernel import GenerationInfo, TurnTraceInfo
from app.modules.observability.public import LangfuseTracer


@pytest.mark.parametrize(
    ("text", "masked"),
    [
        ("Мой телефон +7 (999) 123-45-67, звоните", "Мой телефон [phone], звоните"),
        ("8 999 123 45 67", "[phone]"),
        ("+79991234567", "[phone]"),
        ("тел.: 89991234567.", "тел.: [phone]."),
        ("+46 70 123 45 67", "[phone]"),
        ("пишите на anna.k+shop@mail.ru", "пишите на [email]"),
        ("Telegram: @anna_k", "Telegram: [handle]"),
        ("карта 4111 1111 1111 1111", "карта [card]"),
        ("4111-1111-1111-1111", "[card]"),
    ],
)
def test_contacts_are_masked(text: str, masked: str) -> None:
    assert mask_pii(text) == masked


@pytest.mark.parametrize(
    "text",
    [
        "Цена 3 990 ₽, бесплатно от 3000 ₽",
        "Артикул FR-2402, объём 50 мл",
        "Доставка 2026-10-07 с 10:00 до 14:00",
        "id 392f781e-e4cc-4b92-9127-2987075e4bb9",
        "Заказ хранится 7 дней, интервалы 10:00\u201314:00, 14:00\u201318:00",
        "Почта России: 5\u201314 дней",
        "скидка 15% на второй товар",
    ],
)
def test_ordinary_texts_are_unchanged(text: str) -> None:
    assert mask_pii(text) == text


def test_form_values_and_lead_fields_are_hidden_entirely() -> None:
    data = {
        "type": "form_submit",
        "form_id": "consultation",
        "values": {"name": "Анна Котова", "contact": "+7 999 123-45-67", "comment": ""},
    }
    lead = {"form_key": "consultation", "fields": {"name": "Анна", "time": "вечер"}}

    assert mask_pii(data) == {
        "type": "form_submit",
        "form_id": "consultation",
        "values": {"name": HIDDEN, "contact": HIDDEN, "comment": HIDDEN},
    }
    assert mask_pii(lead) == {
        "form_key": "consultation",
        "fields": {"name": HIDDEN, "time": HIDDEN},
    }


def test_form_submit_in_history_and_json_strings_are_masked() -> None:
    history = '[Отправлена форма consultation: {"name": "Анна", "contact": "@anna_k"}]'
    raw_arguments = json.dumps(
        {"form_key": "consultation", "fields": {"name": "Анна"}}, ensure_ascii=False
    )

    assert (
        mask_pii(history)
        == f'[Отправлена форма consultation: {{"name": "{HIDDEN}", "contact": "{HIDDEN}"}}]'
    )
    assert json.loads(mask_pii(raw_arguments)) == {
        "form_key": "consultation",
        "fields": {"name": HIDDEN},
    }


def test_nested_structures_are_masked_without_changing_input() -> None:
    data: dict[str, Any] = {
        "messages": [{"role": "user", "content": "Звоните +7 999 123-45-67"}],
        "n": 3,
        "ok": True,
    }

    masked = mask_pii(data)

    assert masked == {
        "messages": [{"role": "user", "content": "Звоните [phone]"}],
        "n": 3,
        "ok": True,
    }
    assert data["messages"][0]["content"] == "Звоните +7 999 123-45-67"


def _decoded(attributes: dict[str, Any]) -> dict[str, Any]:
    """Атрибуты SDK — JSON-строки с экранированной кириллицей: раскодировать для проверки."""
    result: dict[str, Any] = {}
    for key, value in attributes.items():
        try:
            result[key] = json.loads(value) if isinstance(value, str) else value
        except ValueError:
            result[key] = value
    return result


def test_langfuse_export_contains_no_contacts() -> None:
    exporter = InMemorySpanExporter()
    tracer = LangfuseTracer.create(
        f"pk-lf-test-{uuid4().hex}",
        "sk-lf-test",
        "http://localhost:9",
        "test",
        span_exporter=exporter,
    )
    phone, email = "+7 999 123-45-67", "anna@mail.ru"
    turn = tracer.start_turn(
        TurnTraceInfo(
            trace_id=uuid4().hex,
            tenant_id=uuid4(),
            conversation_id=uuid4(),
            turn_id=uuid4(),
            input={"type": "form_submit", "form_id": "c", "values": {"name": "Анна"}},
        )
    )
    generation = turn.generation(
        GenerationInfo(
            name="llm",
            model="main",
            input={"messages": [{"role": "user", "content": f"Мой телефон {phone}"}]},
        )
    )
    generation.finish(
        {"tool_calls": [{"arguments": json.dumps({"fields": {"email": email}})}]}, 10, 2
    )
    turn.tool_started("c1", "create_lead")
    turn.tool_finished("c1", "create_lead", {"fields": {"name": "Анна"}}, "ok", None, 3)
    turn.finish({"answer": f"Перезвоним на {phone}"})
    tracer._client.flush()

    exported = json.dumps(
        [_decoded(dict(s.attributes or {})) for s in exporter.get_finished_spans()],
        ensure_ascii=False,
    )
    assert len(exporter.get_finished_spans()) == 3
    for secret in (phone, email, "Анна", "999"):
        assert secret not in exported
    assert "[phone]" in exported and HIDDEN in exported
    tracer.shutdown()


def test_facts_are_hidden_in_arguments_and_in_prompt_state() -> None:
    arguments = {"scenario": "consultation", "facts": ["Имя: Юлия.", "Не любит сладкие [ароматы]"]}
    prompt = (
        "Состояние диалога (уже известно, не переспрашивай):\n"
        + json.dumps(
            {"facts": ["Имя: Юлия.", "Живёт на Тверской]"], "slots": {"budget": "3000"}},
            ensure_ascii=False,
            indent=2,
        )
        + "\nСводка ранней части диалога: нет"
    )

    assert mask_pii(arguments) == {"scenario": "consultation", "facts": [HIDDEN, HIDDEN]}
    masked = mask_pii({"system": prompt})["system"]
    assert "Юлия" not in masked and "Тверской" not in masked
    assert f'"facts": ["{HIDDEN}", "{HIDDEN}"]' in masked
    assert '"budget": "3000"' in masked and masked.endswith("Сводка ранней части диалога: нет")
