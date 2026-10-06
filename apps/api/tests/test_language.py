"""Язык диалога (P6-04a, ADR-0025): выбор по конфигу и locale, тексты тенанта, подписи платформы."""

from typing import Any

import pytest

from app.contracts import AgentConfig, AssistantConfig
from app.modules.tenants.kernel import AssistantTexts, assistant_texts, resolve_language
from app.modules.tools.public import builtin_tools, confirm_labels


def assistant(**fields: Any) -> AssistantConfig:
    return AssistantConfig.model_validate(
        {"name": "A", "greeting": "Привет", "fallback_message": "Ошибка", **fields}
    )


@pytest.mark.parametrize(
    ("fields", "locale", "expected"),
    [
        ({}, "en-US", "en"),
        ({}, "EN", "en"),
        ({}, "sv_SE", "sv"),
        ({}, "ru", "ru"),
        ({}, "de-DE", "ru"),
        ({}, "", "ru"),
        ({}, None, "ru"),
        ({"default_language": "en"}, "fi-FI", "en"),
        ({"language": "en"}, "ru-RU", "en"),
        ({"language": "sv"}, None, "sv"),
    ],
)
def test_resolve_language(fields: dict[str, Any], locale: str | None, expected: str) -> None:
    assert resolve_language(assistant(**fields), locale) == expected


def test_texts_without_translation_are_base_values() -> None:
    texts = assistant_texts(assistant(starter_suggestions=["Подобрать"]), "en")

    assert texts == AssistantTexts(
        greeting="Привет", starter_suggestions=("Подобрать",), fallback_message="Ошибка"
    )


def test_partial_translation_falls_back_to_base_per_field() -> None:
    config = assistant(
        starter_suggestions=["Подобрать"],
        confirm_labels={"confirm": "Да", "cancel": "Нет"},
        translations={"en": {"greeting": "Hi", "confirm_labels": {"confirm": "Yes"}}},
    )

    texts = assistant_texts(config, "en")

    assert (texts.greeting, texts.fallback_message) == ("Hi", "Ошибка")
    assert texts.starter_suggestions == ("Подобрать",)
    assert (texts.confirm, texts.cancel) == ("Yes", "Нет")
    assert assistant_texts(config, "ru").greeting == "Привет"


def test_schema_defaults_of_confirm_labels_are_not_tenant_labels() -> None:
    # «Отмена» — умолчание схемы, а не подпись тенанта: на en её заменит подпись платформы.
    texts = assistant_texts(assistant(confirm_labels={"confirm": "Да"}), "en")

    assert (texts.confirm, texts.cancel) == ("Да", None)
    assert confirm_labels("en", texts.confirm, texts.cancel).cancel == "Cancel"


@pytest.mark.parametrize(
    ("language", "expected"),
    [
        ("ru", ("Подтвердить", "Отмена")),
        ("en", ("Confirm", "Cancel")),
        ("sv", ("Bekräfta", "Avbryt")),
    ],
)
def test_platform_confirm_labels(language: str, expected: tuple[str, str]) -> None:
    labels = confirm_labels(language)

    assert (labels.confirm, labels.cancel) == expected


class _Stub:
    """Зависимости инструментов: в тесте подписей они не вызываются."""


ALL_BUILTIN = [
    "update_dialog_state",
    "search_knowledge",
    "search_catalog",
    "get_entity",
    "show_entities",
    "suggest_replies",
    "show_form",
    "create_lead",
]


def _all_builtin(language: str) -> dict[str, str | None]:
    config = AgentConfig.model_validate(
        {
            "assistant": {"name": "A", "greeting": "", "fallback_message": ""},
            "model": {"primary": {"provider": "openai", "name": "m"}},
            "limits": {},
            "prompt": {"tenant": "t"},
            "tools": {"builtin": ALL_BUILTIN},
            "forms": {
                "contact": {"title": "К", "fields": [{"name": "p", "label": "Т", "kind": "text"}]}
            },
        }
    )
    stub: Any = _Stub()
    tools = builtin_tools(config, knowledge=stub, catalog=stub, leads=stub, language=language)
    return {t.name: t.display_label for t in tools}


@pytest.mark.parametrize("language", ["en", "sv"])
def test_every_builtin_display_label_is_translated(language: str) -> None:
    russian = _all_builtin("ru")
    translated = _all_builtin(language)

    assert set(translated) == set(ALL_BUILTIN)
    for name, label in russian.items():
        if label is None:
            assert translated[name] is None
        else:
            assert translated[name] not in (None, label), name
