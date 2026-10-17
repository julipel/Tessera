"""Встроенные инструменты `show_form` и `create_lead` (contracts.md §3, §4, ADR-0021): формы
из `forms` конфига тенанта и заявка по полям формы. Заявка — побочный эффект: создаётся
только после подтверждения пользователя.
"""

import re
from collections.abc import Mapping
from typing import Any

from app.contracts import Form, FormConfig
from app.modules.tools.domain.definition import ToolContext, ToolDefinition
from app.modules.tools.domain.ports import LeadStore
from app.modules.tools.domain.result import ToolError, ToolResult

SHOW_FORM = "show_form"
CREATE_LEAD = "create_lead"

# Длина значения поля формы (REVIEW.md: L-6); те же числа — maxLength полей в виджете
# (apps/web/src/components/rich/Forms.tsx).
MAX_TEXTAREA_LENGTH = 2000
MAX_FIELD_LENGTH = 200

# Управляющие символы, кроме табуляции и переводов строки: \u0000 Postgres не принимает
# в JSONB, остальные в заявке не нужны (REVIEW.md: L-2).
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
# Мягкая проверка формата: телефон — цифры и разделители, от 7 до 15 цифр (E.164 — до 15);
# e-mail — «имя@домен.зона» без пробелов.
_PHONE = re.compile(r"\+?[\d\s().\-]+")
_EMAIL = re.compile(r"[^@\s]+@[^@\s]+\.[^@\s.]+")

_SHOW_FORM_DESCRIPTION = (
    "Покажи пользователю форму из настроек: например, чтобы оставить контакт. Значения "
    "пользователь заполнит и отправит сам — придут следующим сообщением. Не проси вводить "
    "те же данные текстом."
)
_CREATE_LEAD_DESCRIPTION = (
    "Создай заявку по полям формы: значения — только те, что сообщил пользователь (из "
    "отправленной формы или из диалога). Перед созданием пользователь увидит заявку и "
    "подтвердит её кнопкой; до подтверждения заявка не создана."
)


def show_form_tool(forms: Mapping[str, FormConfig], submit_label: str) -> ToolDefinition:
    """`submit_label` — кнопка отправки на языке диалога (`labels.submit_label`)."""

    async def handler(arguments: Mapping[str, Any], ctx: ToolContext, /) -> ToolResult:
        key = arguments["form_key"]
        form = forms[key]
        component = Form(
            type="form",
            form_id=key,
            title=form.title,
            fields=form.fields,
            submit_label=submit_label,
        )
        return ToolResult(
            content=f"Форма «{form.title}» показана; дождись, пока пользователь её отправит.",
            components=(component.model_dump(mode="json", exclude_none=True),),
        )

    return ToolDefinition(
        name=SHOW_FORM,
        description=_SHOW_FORM_DESCRIPTION,
        parameters={
            "type": "object",
            "properties": {"form_key": _form_key_schema(forms)},
            "required": ["form_key"],
            "additionalProperties": False,
        },
        handler=handler,
        timeout_s=1,
    )


def create_lead_tool(forms: Mapping[str, FormConfig], leads: LeadStore) -> ToolDefinition:
    """Схема `fields` — объединение полей всех форм (поле с одним именем берётся из первой
    формы); принадлежность полей выбранной форме и обязательность проверяет `check`:
    условные схемы не все провайдеры принимают."""
    properties: dict[str, Any] = {}
    for form in forms.values():
        for field in form.fields:
            properties.setdefault(
                field.name,
                {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": _max_length(field.kind),
                    "description": field.label,
                },
            )

    async def handler(arguments: Mapping[str, Any], ctx: ToolContext, /) -> ToolResult:
        if ctx.confirm_id is None:  # реестр исполняет create_lead только после подтверждения
            return ToolResult(
                error=ToolError(
                    code="validation_error",
                    message=f"{CREATE_LEAD}: заявка создаётся только после подтверждения",
                    retryable=False,
                )
            )
        form = forms[arguments["form_key"]]
        lead = await leads.create(
            ctx.tenant_id,
            ctx.conversation_id,
            arguments["form_key"],
            arguments["fields"],
            idempotency_key=ctx.confirm_id,
            form_title=form.title,
            field_labels={f.name: f.label for f in form.fields},
        )
        return ToolResult(
            content=f"Заявка создана, номер {lead.number}. Сообщи номер и что будет "
            "дальше — по базе знаний, если там это описано; сроки не выдумывай."
        )

    return ToolDefinition(
        name=CREATE_LEAD,
        description=_CREATE_LEAD_DESCRIPTION,
        parameters={
            "type": "object",
            "properties": {
                "form_key": _form_key_schema(forms),
                "fields": {
                    "type": "object",
                    "properties": properties,
                    "additionalProperties": False,
                },
            },
            "required": ["form_key", "fields"],
            "additionalProperties": False,
        },
        handler=handler,
        side_effect=True,
        requires_confirmation=True,
        display_label="Оформляю заявку",
        check=lambda arguments: form_values_problem(
            forms[arguments["form_key"]], arguments["fields"]
        ),
        confirm_text=lambda arguments: _summary(forms[arguments["form_key"]], arguments["fields"]),
    )


def form_values_problem(form: FormConfig, values: Mapping[str, Any]) -> str | None:
    """Почему значения не подходят форме (лишнее поле, пустое обязательное, не строка,
    длиннее лимита, управляющие символы, не телефон/e-mail по виду, нет в вариантах select)
    или None. Общая проверка `create_lead` и отправки формы."""
    fields = {field.name: field for field in form.fields}
    if unknown := sorted(set(values) - set(fields)):
        return f"в форме «{form.title}» нет полей: {', '.join(unknown)}"
    for name, field in fields.items():
        value = values.get(name)
        if value is not None and not isinstance(value, str):
            return f"поле {name}: значение должно быть строкой"
        if field.required and not (value or "").strip():
            return f"не заполнено обязательное поле {name} ({field.label})"
        if not value:
            continue
        if len(value) > (limit := _max_length(field.kind)):
            return f"поле {name}: не длиннее {limit} символов"
        if _CONTROL.search(value):
            return f"поле {name}: недопустимые символы"
        if field.kind == "phone" and not _is_phone(value):
            return f"поле {name} ({field.label}): не похоже на номер телефона"
        if field.kind == "email" and not _EMAIL.fullmatch(value.strip()):
            return f"поле {name} ({field.label}): не похоже на e-mail"
        if field.kind == "select" and value not in {o.value for o in field.options or ()}:
            return f"поле {name}: значение не из вариантов"
    return None


def _max_length(kind: str) -> int:
    return MAX_TEXTAREA_LENGTH if kind == "textarea" else MAX_FIELD_LENGTH


def _is_phone(value: str) -> bool:
    value = value.strip()
    return bool(_PHONE.fullmatch(value)) and 7 <= sum(c.isdigit() for c in value) <= 15


def _summary(form: FormConfig, values: Mapping[str, str]) -> str:
    filled = [f"{f.label}: {values[f.name]}" for f in form.fields if values.get(f.name)]
    return f"{form.title}. {'; '.join(filled)}"


def _form_key_schema(forms: Mapping[str, FormConfig]) -> dict[str, Any]:
    return {
        "type": "string",
        "enum": list(forms),
        "description": "; ".join(f"{key} — {form.title}" for key, form in forms.items()),
    }
