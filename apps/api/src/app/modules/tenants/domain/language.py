"""Язык диалога и тексты тенанта на нём (ADR-0025).

Язык выбирается один раз — при создании диалога (и для public config до него): фиксированный
`assistant.language` или, при `auto`, основной язык из `locale` клиента, если платформа его
поддерживает; иначе — `assistant.default_language`, язык текстов тенанта.
"""

from dataclasses import dataclass
from typing import Literal

from app.contracts import AssistantConfig, ConfirmLabels

type Language = Literal["ru", "en", "sv"]

_LANGUAGES: dict[str, Language] = {"ru": "ru", "en": "en", "sv": "sv"}


def resolve_language(assistant: AssistantConfig, locale: str | None = None) -> Language:
    """Язык диалога. `locale` — BCP 47 (`en-US`, `sv`), регистр и `_` допустимы."""
    fixed = _LANGUAGES.get(assistant.language or "auto")
    if fixed is not None:
        return fixed
    if locale:
        primary = _LANGUAGES.get(locale.replace("_", "-").split("-")[0].strip().lower())
        if primary is not None:
            return primary
    return default_language(assistant)


def default_language(assistant: AssistantConfig) -> Language:
    return assistant.default_language or "ru"


@dataclass(frozen=True, slots=True)
class AssistantTexts:
    """Тексты тенанта на языке диалога: перевод поля из `translations`, иначе базовое
    значение. `confirm`/`cancel` — только заданные тенантом подписи кнопок confirm; None —
    подпись платформы на языке диалога."""

    greeting: str
    starter_suggestions: tuple[str, ...]
    fallback_message: str
    confirm: str | None = None
    cancel: str | None = None


def assistant_texts(assistant: AssistantConfig, language: Language) -> AssistantTexts:
    translation = getattr(assistant.translations, language, None)
    greeting = assistant.greeting
    suggestions = assistant.starter_suggestions or []
    fallback = assistant.fallback_message
    confirm, cancel = _set_labels(assistant.confirm_labels)
    if translation is not None:
        greeting = translation.greeting if translation.greeting is not None else greeting
        if translation.starter_suggestions is not None:
            suggestions = translation.starter_suggestions
        if translation.fallback_message is not None:
            fallback = translation.fallback_message
        confirm_tr, cancel_tr = _set_labels(translation.confirm_labels)
        confirm, cancel = confirm_tr or confirm, cancel_tr or cancel
    return AssistantTexts(
        greeting=greeting,
        starter_suggestions=tuple(suggestions),
        fallback_message=fallback,
        confirm=confirm,
        cancel=cancel,
    )


def _set_labels(labels: ConfirmLabels | None) -> tuple[str | None, str | None]:
    # Умолчания схемы («Подтвердить»/«Отмена») — русские: берём только явно заданные поля,
    # остальное подставит платформа на языке диалога.
    if labels is None:
        return None, None
    confirm = labels.confirm if "confirm" in labels.model_fields_set else None
    cancel = labels.cancel if "cancel" in labels.model_fields_set else None
    return confirm, cancel
