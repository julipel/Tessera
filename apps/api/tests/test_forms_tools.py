"""show_form и create_lead (P5-04a, ADR-0021): формы из конфига, заявка — после подтверждения."""

from collections.abc import Mapping
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


class Leads:
    def __init__(self) -> None:
        self.created: list[tuple[TenantId, UUID, str, dict[str, str]]] = []

    async def create(
        self, tenant_id: TenantId, conversation_id: UUID, form_key: str, fields: Mapping[str, str]
    ) -> UUID:
        self.created.append((tenant_id, conversation_id, form_key, dict(fields)))
        return UUID("12345678-0000-0000-0000-000000000000")


async def show(arguments: dict[str, Any]) -> ToolResult:
    [result] = await ToolRegistry([show_form_tool(FORMS)]).execute_many(
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
    assert list(properties["fields"]["properties"]) == ["name", "contact", "comment", "time"]
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

    assert result.content == "Заявка создана, номер 12345678."
    assert leads.created == [(CTX.tenant_id, CTX.conversation_id, "consultation", ANNA)]


@pytest.mark.parametrize(
    ("form_key", "fields", "message"),
    [
        ("consultation", {"name": "Анна"}, "обязательное поле contact"),
        ("consultation", {**ANNA, "time": "morning"}, "нет полей: time"),
        ("callback", {"contact": "+7900", "time": "night"}, "не из вариантов"),
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
