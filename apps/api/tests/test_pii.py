"""Маскирование персональных данных в трейсах Langfuse (P7-07, ADR-0032)."""

import gc
import json
import random
import time
from typing import Any
from uuid import uuid4

import pytest
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from app.modules.observability.domain.pii import HIDDEN, _LuhnPrefix, has_contact, mask_pii
from app.modules.observability.kernel import GenerationInfo, GenerationUsage, TurnTraceInfo
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
        # Соседнее число не прячет телефон.
        (
            "Мой номер 89991112233 15.10.2026 перезвоните",
            "Мой номер [phone] 15.10.2026 перезвоните",
        ),
        ("8 999 111 22 33 8 999 444 55 66", "[phone] [phone]"),
        ("4111 1111 1111 1111 89991112233", "[card] [phone]"),
        # Четыре группы по 4 — карта, хотя сумма Луна не сходится (опечатка; O-10).
        ("1234 5678 9012 3456", "[card]"),
        ("карта 1234-5678-9012-3456.", "карта [card]."),
        # Тонкие пробелы, неразрывный дефис и тире как разделители (O-10).
        ("+7\u2009999\u2009111\u201122\u201133", "[phone]"),
        ("8\u202f999\u202f111\u202f22\u202f33", "[phone]"),
        ("4111\u20091111\u20091111\u20091111", "[card]"),
        # Телефон вплотную к русскому слову (O-10).
        ("звони89991112233", "звони[phone]"),
        ("89991112233позвоните", "[phone]позвоните"),
        ("тел:+79991112233", "тел:[phone]"),
        # Ссылки на Telegram (O-10).
        ("пишите в t.me/anna_k", "пишите в [handle]"),
        ("https://t.me/anna_k_shop?start=1", "[handle]?start=1"),
        ("TELEGRAM.ME/Anna_K", "[handle]"),
        ("чат t.me/+AbCdEf12 и t.me/joinchat/abc", "чат [handle] и [handle]"),
    ],
)
def test_contacts_are_masked(text: str, masked: str) -> None:
    assert mask_pii(text) == masked
    assert has_contact(text)


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
        "Доставка 15.10.2026 10.11.2026",
        "8 999 123 45 67x",
        "Артикул AB89991112233",
        "сорт Snow\u2009White, высота 40\u201360 см",
        "номер заказа 1234567890123456",
        "https://example.ru/t.me/x",
        # Диапазоны через тире — не телефоны (тире не разделитель номера).
        "бюджет 10 000\u201315 000 \u20bd",
        "бюджет 50 000\u2013100 000",
        "300 000\u2014500 000 \u20bd",
        "бюджет 9\u202f000\u202f000\u201312\u202f000\u202f000",
        "диапазон 1000\u20132000\u20133000\u20134000",
    ],
)
def test_ordinary_texts_are_unchanged(text: str) -> None:
    assert mask_pii(text) == text
    assert not has_contact(text)


@pytest.mark.parametrize(
    "text",
    [
        "a" * 20_000,
        "1." * 10_000,
        "a@" * 10_000,
        "1 " * 10_000,
        "anna" + "." * 20_000,
        "1\u2009" * 10_000,
        "1234 " * 5_000,
        "t.me/" * 5_000,
        "https://t.me" * 2_000,
        '"slots": {' * 5_000,
        '"slots": {"a": [' * 3_000,
        '"facts": [1, ' * 20_000,
    ],
)
def test_masking_time_is_linear(text: str) -> None:
    """Маскирование синхронно в корутине хода: длинный текст не должен её блокировать.
    Сборщик мусора на время замера выключен: в полном прогоне pytest куча большая, и его
    паузы (сотни мс) — не сложность алгоритма."""
    gc.collect()
    gc.disable()
    try:
        started = time.perf_counter()
        mask_pii(text)
        elapsed = time.perf_counter() - started
    finally:
        gc.enable()
    assert elapsed < 0.2


def test_luhn_of_any_substring_matches_direct_check() -> None:
    rng = random.Random(7)
    for _ in range(5000):
        digits = "".join(rng.choice("0123456789") for _ in range(rng.randint(1, 40)))
        start = rng.randrange(len(digits))
        end = rng.randint(start + 1, len(digits))
        assert _LuhnPrefix(digits).valid(start, end) is _luhn_direct(digits[start:end])


def _luhn_direct(digits: str) -> bool:
    total = 0
    for i, char in enumerate(reversed(digits)):
        d = int(char)
        if i % 2:
            d = d * 2 - 9 if d > 4 else d * 2
        total += d
    return total % 10 == 0


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
        {"tool_calls": [{"arguments": json.dumps({"fields": {"email": email}})}]},
        GenerationUsage(10, 2),
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
    # Не просто "999": такая подстрока бывает в случайных UUID трейса.
    for secret in (phone, email, "Анна", "999 123"):
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
    assert f'"budget": "{HIDDEN}"' in masked and masked.endswith("Сводка ранней части диалога: нет")


def test_slot_values_are_hidden_with_names_kept() -> None:
    """Слоты — адреса, имена получателей (O-10): значения скрыты, названия видны."""
    arguments = {"slots": {"location": "Мытищи, ул. Лесная 5", "plot_size_sotki": 6, "goal": ""}}
    state = {"slots": {"landscape_visit": {"location": "Мытищи", "tags": ["газон", "сад"]}}}
    prompt = "Состояние:\n" + json.dumps(
        {"slots": {"location": "ул. Лесная }5"}, "active_scenario": "landscape_visit"},
        ensure_ascii=False,
        indent=2,
    )

    assert mask_pii(arguments) == {
        "slots": {"location": HIDDEN, "plot_size_sotki": HIDDEN, "goal": ""}
    }
    assert mask_pii(state) == {
        "slots": {"landscape_visit": {"location": HIDDEN, "tags": [HIDDEN] * 2}}
    }
    masked = mask_pii(prompt)
    assert "Лесная" not in masked
    assert f'"slots": {{"location": "{HIDDEN}"}}' in masked
    assert '"active_scenario": "landscape_visit"' in masked
    assert mask_pii(json.dumps(arguments, ensure_ascii=False)) == json.dumps(
        mask_pii(arguments), ensure_ascii=False
    )


@pytest.mark.parametrize(
    ("value", "masked"),
    [(89991112233, "[phone]"), (4111111111111111, "[card]"), (79991112233, "[phone]")],
)
def test_contacts_as_numbers_are_masked(value: int, masked: str) -> None:
    """Модель присылает телефон числом в аргументах инструмента (O-10)."""
    assert mask_pii({"phone": value}) == {"phone": masked}
    assert mask_pii(json.dumps({"phone": value})) == json.dumps({"phone": masked})


@pytest.mark.parametrize("value", [3990, 123456789, 1234567890123456, -89991112233, True])
def test_other_numbers_are_unchanged(value: int) -> None:
    """Короткие числа, 16 цифр без суммы Луна (номер заказа), отрицательные и bool — как есть."""
    assert mask_pii(value) == value


def test_json_array_in_string_is_masked() -> None:
    text = json.dumps(
        [{"fields": {"name": "Анна"}}, "звоните +7 999 111 22 33"], ensure_ascii=False
    )

    masked = json.loads(mask_pii(text))

    assert masked == [{"fields": {"name": HIDDEN}}, "звоните [phone]"]


@pytest.mark.parametrize(
    "text",
    [
        'Состояние: "slots": {"x": 1, "facts": ["Юлия, Тверская 5"]',
        '{"slots": {"addr": "Тверская 5", "facts": ["Юлия"]...',
        'Состояние: "facts": ["Юлия", "Тверская 5"\nСводка: нет',
    ],
)
def test_broken_state_json_is_hidden(text: str) -> None:
    """Битый или обрезанный JSON состояния скрывается, а не пропускается (O-10)."""
    masked = mask_pii(text)

    assert "Юлия" not in masked and "Тверская" not in masked
    assert HIDDEN in masked


def test_many_broken_state_parts_hide_the_rest() -> None:
    text = '"facts": [1, ' * 30 + "Юлия"

    masked = mask_pii(text)

    assert "Юлия" not in masked and masked.endswith(f'"{HIDDEN}"')


def test_too_deep_state_json_hides_the_rest() -> None:
    text = 'Состояние: "slots": ' + '{"a": ' * 100_000 + '"Лесная 5"' + "}" * 100_000

    assert mask_pii(text) == f'Состояние: "slots": "{HIDDEN}"'
    deep = '{"a": ' * 100_000 + '{"fields": {"name": "Анна"}}' + "}" * 100_000
    assert mask_pii(deep) == HIDDEN


def test_tuple_stays_tuple() -> None:
    """Докстринг обещает копию: тип контейнера сохраняется (O-15)."""
    assert mask_pii(("+7 999 111 22 33", 1)) == ("[phone]", 1)
