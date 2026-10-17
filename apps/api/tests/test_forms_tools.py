"""show_form и create_lead (P5-04a, ADR-0021): формы из конфига, заявка — после подтверждения."""

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any
from uuid import UUID, uuid4

import pytest

from app.contracts import AgentConfig, FormConfig
from app.modules.shared.kernel import TenantId
from app.modules.tools.public import (
    CREATE_LEAD,
    SHOW_FORM,
    ToolContext,
    ToolInvocation,
    ToolRegistry,
    ToolResult,
    builtin_tools,
    create_lead_tool,
    form_values_problem,
    show_form_tool,
)

FORMS = {
    "consultation": FormConfig.model_validate(
        {
            "title": "Консультация косметолога",
            "fields": [
                {"name": "name", "label": "Имя", "kind": "text", "required": True},
                {
                    "name": "contact",
                    "label": "Телефон или Telegram",
                    "kind": "text",
                    "required": True,
                },
                {"name": "comment", "label": "Комментарий", "kind": "textarea"},
            ],
        }
    ),
    "callback": FormConfig.model_validate(
        {
            "title": "Обратный звонок",
            "fields": [
                {"name": "contact", "label": "Телефон", "kind": "phone", "required": True},
                {
                    "name": "time",
                    "label": "Когда позвонить",
                    "kind": "select",
                    "options": [
                        {"label": "Утром", "value": "morning"},
                        {"label": "Вечером", "value": "evening"},
                    ],
                },
            ],
        }
    ),
}
CTX = ToolContext(tenant_id=TenantId(uuid4()), conversation_id=uuid4(), turn_id=uuid4())
ANNA = {"name": "Анна", "contact": "@anna"}


@dataclass(frozen=True, slots=True)
class Created:
    id: UUID
    number: int


class Leads:
    def __init__(self) -> None:
        self.created: list[tuple[TenantId, UUID, str, dict[str, str]]] = []
        self.keys: list[str] = []
        self.labels: list[tuple[str, dict[str, str]]] = []

    async def create(
        self,
        tenant_id: TenantId,
        conversation_id: UUID,
        form_key: str,
        fields: Mapping[str, str],
        *,
        idempotency_key: str,
        form_title: str,
        field_labels: Mapping[str, str],
    ) -> Created:
        self.created.append((tenant_id, conversation_id, form_key, dict(fields)))
        self.keys.append(idempotency_key)
        self.labels.append((form_title, dict(field_labels)))
        return Created(id=uuid4(), number=42)


async def show(arguments: dict[str, Any]) -> ToolResult:
    [result] = await ToolRegistry([show_form_tool(FORMS, "Отправить")]).execute_many(
        [ToolInvocation(id="c1", name=SHOW_FORM, arguments=arguments)], CTX
    )
    return result


def lead(form_key: str, fields: dict[str, Any]) -> ToolInvocation:
    return ToolInvocation(
        id="c1", name=CREATE_LEAD, arguments={"form_key": form_key, "fields": fields}
    )


async def test_show_form_emits_form_component_from_config() -> None:
    result = await show({"form_key": "callback"})

    assert result.ok
    assert result.components == (
        {
            "type": "form",
            "form_id": "callback",
            "title": "Обратный звонок",
            "fields": [
                {"name": "contact", "label": "Телефон", "kind": "phone", "required": True},
                {
                    "name": "time",
                    "label": "Когда позвонить",
                    "kind": "select",
                    "required": False,
                    "options": [
                        {"label": "Утром", "value": "morning"},
                        {"label": "Вечером", "value": "evening"},
                    ],
                },
            ],
            "submit_label": "Отправить",
        },
    )


async def test_show_form_rejects_unknown_form() -> None:
    result = await show({"form_key": "order"})

    assert result.error is not None and result.error.code == "validation_error"


def test_create_lead_schema_lists_forms_and_fields_of_all_forms() -> None:
    tool = create_lead_tool(FORMS, Leads())
    properties = tool.parameters["properties"]

    assert properties["form_key"]["enum"] == ["consultation", "callback"]
    fields = properties["fields"]["properties"]
    assert list(fields) == ["name", "contact", "comment", "time"]
    # Лимит длины — и в схеме для модели, и в проверке значений (L-6).
    assert (fields["name"]["maxLength"], fields["comment"]["maxLength"]) == (200, 2000)
    assert (tool.side_effect, tool.requires_confirmation) == (True, True)


async def test_create_lead_asks_confirmation_with_summary_and_creates_nothing() -> None:
    leads = Leads()

    [result] = await ToolRegistry([create_lead_tool(FORMS, leads)]).execute_many(
        [lead("consultation", ANNA)], CTX
    )

    assert leads.created == []
    [confirm] = result.components
    assert confirm["text"] == "Консультация косметолога. Имя: Анна; Телефон или Telegram: @anna"
    assert result.state_patch is not None
    assert result.state_patch["pending_confirmation"]["arguments"] == {
        "form_key": "consultation",
        "fields": ANNA,
    }


async def test_confirmed_create_lead_stores_lead_of_context_tenant() -> None:
    leads = Leads()

    result = await ToolRegistry([create_lead_tool(FORMS, leads)]).execute_confirmed(
        lead("consultation", ANNA), CTX
    )

    assert isinstance(result.content, str)
    assert result.content.startswith("Заявка создана, номер 42.")
    assert leads.created == [(CTX.tenant_id, CTX.conversation_id, "consultation", ANNA)]
    # Ключ идемпотентности — confirm_id подтверждения (id подтверждённого вызова).
    assert leads.keys == ["c1"]
    # Заголовок и подписи — из формы конфига на момент отправки (ADR-0045), в порядке формы.
    form = FORMS["consultation"]
    assert leads.labels == [(form.title, {f.name: f.label for f in form.fields})]
    assert list(leads.labels[0][1]) == [f.name for f in form.fields]


class BrokenLeads(Leads):
    async def create(self, *args: Any, **kwargs: Any) -> Created:
        raise TimeoutError("QueuePool limit reached")


async def test_lead_store_failure_after_confirmation_is_reported_without_retry() -> None:
    # Сбой записи заявки (REVIEW.md: TO-12): модели — ошибка без внутренностей и без
    # повтора (исход неизвестен), заявки нет.
    leads = BrokenLeads()

    result = await ToolRegistry([create_lead_tool(FORMS, leads)]).execute_confirmed(
        lead("consultation", ANNA), CTX
    )

    assert result.error is not None
    assert (result.error.code, result.error.retryable) == ("upstream_error", False)
    assert "QueuePool" not in result.error.message
    assert leads.created == []


async def test_second_confirmation_in_one_step_is_rejected() -> None:
    # Два create_lead за шаг (REVIEW.md: AG-12, TO-12): ожидание — от первого, второй —
    # ошибка без компонента; заявок нет.
    leads = Leads()
    second = ToolInvocation(
        id="c2",
        name=CREATE_LEAD,
        arguments={"form_key": "callback", "fields": {"contact": "+79001112233"}},
    )

    first, rejected = await ToolRegistry([create_lead_tool(FORMS, leads)]).execute_many(
        [lead("consultation", ANNA), second], CTX
    )

    assert first.state_patch is not None
    assert first.state_patch["pending_confirmation"]["arguments"]["form_key"] == "consultation"
    assert len(first.components) == 1
    # forbidden, а не validation_error: ретраи хода на отказ не тратятся.
    assert rejected.error is not None and rejected.error.code == "forbidden"
    assert "уже показано подтверждение" in rejected.error.message
    assert (rejected.components, rejected.state_patch) == ((), None)
    assert leads.created == []


async def test_create_lead_without_confirmation_does_not_store_lead() -> None:
    leads = Leads()
    tool = create_lead_tool(FORMS, leads)

    result = await tool.handler(lead("consultation", ANNA).arguments, CTX)

    assert result.error is not None
    assert result.error.code == "validation_error"
    assert leads.created == []


@pytest.mark.parametrize(
    ("form_key", "fields", "message"),
    [
        ("consultation", {"name": "Анна"}, "обязательное поле contact"),
        ("consultation", {**ANNA, "time": "morning"}, "нет полей: time"),
        ("callback", {"contact": "+7 900 111-22-33", "time": "night"}, "не из вариантов"),
        ("callback", {"contact": "@anna"}, "не похоже на номер телефона"),
    ],
)
async def test_create_lead_fields_are_checked_against_chosen_form(
    form_key: str, fields: dict[str, Any], message: str
) -> None:
    [result] = await ToolRegistry([create_lead_tool(FORMS, Leads())]).execute_many(
        [lead(form_key, fields)], CTX
    )

    assert result.error is not None and result.error.code == "validation_error"
    assert message in result.error.message
    assert result.components == ()


@pytest.mark.parametrize(
    ("values", "problem"),
    [
        (ANNA, None),
        ({**ANNA, "comment": ""}, None),
        (
            {"name": "Анна", "contact": "  "},
            "не заполнено обязательное поле contact (Телефон или Telegram)",
        ),
        ({**ANNA, "name": 5}, "поле name: значение должно быть строкой"),
    ],
)
def test_form_values_problem(values: dict[str, Any], problem: str | None) -> None:
    assert form_values_problem(FORMS["consultation"], values) == problem


CONTACTS = FormConfig.model_validate(
    {
        "title": "Контакты",
        "fields": [
            {"name": "phone", "label": "Телефон", "kind": "phone"},
            {"name": "email", "label": "E-mail", "kind": "email"},
            {"name": "name", "label": "Имя", "kind": "text"},
            {"name": "comment", "label": "Комментарий", "kind": "textarea"},
        ],
    }
)


@pytest.mark.parametrize(
    "values",
    [
        {"phone": "+7 (999) 111-22-33"},
        {"phone": "89991112233"},
        {"phone": " 8.999.111.22.33 "},
        {"phone": "1112233"},
        {"email": "anna.k+spa@mail.example.ru"},
        {"name": "x" * 200, "comment": "строка\nвторая\tс табом\r\n" + "я" * 1900},
        # Поле kind: text не проверяется по виду: «телефон или Telegram» одним полем.
        {"name": "@anna"},
    ],
)
def test_form_values_accept_contacts_in_usual_formats(values: dict[str, str]) -> None:
    assert form_values_problem(CONTACTS, values) is None


@pytest.mark.parametrize(
    ("values", "problem"),
    [
        ({"phone": "@anna"}, "phone (Телефон): не похоже на номер телефона"),
        ({"phone": "+7 900"}, "phone (Телефон): не похоже на номер телефона"),
        ({"phone": "1" * 16}, "phone (Телефон): не похоже на номер телефона"),
        ({"phone": "+7 999 111 22 33 доб. 5"}, "phone (Телефон): не похоже на номер телефона"),
        ({"email": "anna@mail"}, "email (E-mail): не похоже на e-mail"),
        ({"email": "anna @mail.ru"}, "email (E-mail): не похоже на e-mail"),
        ({"email": "@mail.ru"}, "email (E-mail): не похоже на e-mail"),
        ({"name": "x" * 201}, "поле name: не длиннее 200 символов"),
        ({"comment": "x" * 2001}, "поле comment: не длиннее 2000 символов"),
        ({"name": "Анна\u0000"}, "поле name: недопустимые символы"),
        ({"comment": "текст\x1b[31m"}, "поле comment: недопустимые символы"),
    ],
)
def test_form_values_reject_bad_formats(values: dict[str, str], problem: str) -> None:
    result = form_values_problem(CONTACTS, values)
    assert result is not None and result.endswith(problem)


def config(builtin: list[str], forms: dict[str, Any] | None) -> AgentConfig:
    return AgentConfig.model_validate(
        {
            "assistant": {"name": "A", "greeting": "Привет", "fallback_message": "Ошибка"},
            "model": {"primary": {"provider": "openai", "name": "m"}},
            "limits": {},
            "prompt": {"tenant": "Ты — консультант."},
            "tools": {"builtin": builtin},
            **({"forms": {k: f.model_dump() for k, f in FORMS.items()}} if forms else {}),
        }
    )


def test_form_tools_need_forms_in_config_and_lead_store() -> None:
    enabled = [SHOW_FORM, CREATE_LEAD]

    assert [t.name for t in builtin_tools(config(enabled, FORMS), leads=Leads())] == enabled
    assert [t.name for t in builtin_tools(config(enabled, FORMS))] == [SHOW_FORM]
    assert builtin_tools(config(enabled, None), leads=Leads()) == []


@pytest.mark.parametrize(
    ("language", "label"), [("ru", "Отправить"), ("en", "Send"), ("sv", "Skicka")]
)
async def test_form_submit_label_follows_dialog_language(language: str, label: str) -> None:
    """TO-1: кнопка формы — на языке диалога, а не умолчание контракта «Отправить»."""
    [tool] = builtin_tools(config([SHOW_FORM], FORMS), language=language)
    [result] = await ToolRegistry([tool]).execute_many(
        [ToolInvocation(id="c1", name=SHOW_FORM, arguments={"form_key": "callback"})], CTX
    )

    assert result.components[0]["submit_label"] == label
