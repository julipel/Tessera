"""Язык диалога и тексты тенанта на нём (ADR-0025).

Язык выбирается один раз — при создании диалога (и для public config до него): фиксированный
`assistant.language` или, при `auto`, основной язык из `locale` клиента, если платформа его
поддерживает; иначе — `assistant.default_language`, язык текстов тенанта.
"""

from dataclasses import dataclass
from typing import Literal

from app.contracts import (
    AgentConfig,
    AssistantConfig,
    CardAction,
    ConfirmLabels,
    FormConfig,
    FormField,
    FormFieldTranslation,
    FormTranslation,
)

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


def localize_config(config: AgentConfig, language: Language) -> AgentConfig:
    """Копия конфига, где тексты тенанта, которые видит клиент в компонентах инструментов, —
    на языке диалога: `forms` (title, label полей и вариантов select), кнопки и подписи
    атрибутов каталога. Переводы — по ключам (`form_key`, `name`, `value`, `action_id`, ключ
    атрибута), а не по позиции; без перевода — базовое значение. Ключи и значения форм не
    меняются: отправленную форму проверяют по базовому конфигу."""
    translation = getattr(config.assistant.translations, language, None)
    if translation is None:
        return config
    update: dict[str, object] = {}
    if config.forms and translation.forms:
        update["forms"] = {
            key: _localize_form(form, translation.forms.get(key))
            for key, form in config.forms.items()
        }
    catalog = config.knowledge.catalog if config.knowledge else None
    if catalog is not None and config.knowledge is not None:
        actions = translation.card_actions or {}
        attributes = translation.attribute_labels or {}
        localized = catalog.model_copy(
            update={
                "card_actions": [
                    _localize_action(action, actions.get(action.action_id))
                    for action in catalog.card_actions or ()
                ],
                "attribute_labels": {
                    key: attributes.get(key, label)
                    for key, label in (catalog.attribute_labels or {}).items()
                },
            }
        )
        update["knowledge"] = config.knowledge.model_copy(update={"catalog": localized})
    return config.model_copy(update=update) if update else config


def _localize_form(form: FormConfig, translation: FormTranslation | None) -> FormConfig:
    if translation is None:
        return form
    fields = translation.fields or {}
    return form.model_copy(
        update={
            "title": translation.title if translation.title is not None else form.title,
            "fields": [_localize_field(field, fields.get(field.name)) for field in form.fields],
        }
    )


def _localize_field(field: FormField, translation: FormFieldTranslation | None) -> FormField:
    if translation is None:
        return field
    options = translation.options or {}
    return field.model_copy(
        update={
            "label": translation.label or field.label,
            "options": None
            if field.options is None
            else [
                option.model_copy(update={"label": options.get(option.value, option.label)})
                for option in field.options
            ],
        }
    )


def _localize_action(action: CardAction, label: str | None) -> CardAction:
    return action if label is None else action.model_copy(update={"label": label})
