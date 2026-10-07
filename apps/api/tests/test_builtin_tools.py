"""Встроенные инструменты (P2-09, P6-01, P6-05): update_dialog_state — схема по слотам
сценариев, выбор сценария, слоты только сценария вызова и state_patch."""

from typing import Any
from uuid import uuid4

import pytest

from app.contracts import AgentConfig, ScenarioConfig
from app.modules.shared.kernel import TenantId
from app.modules.tools.public import (
    SUGGEST_REPLIES,
    UPDATE_DIALOG_STATE,
    ToolContext,
    ToolInvocation,
    ToolRegistry,
    ToolResult,
    builtin_tools,
    suggest_replies_tool,
    update_dialog_state_tool,
)

SCENARIOS = [
    ScenarioConfig.model_validate(
        {
            "key": "skincare",
            "description": "Уход за кожей",
            "instructions": "Выясни тип кожи.",
            "slots": {
                "skin_type": {"type": "string", "enum": ["сухая", "жирная"]},
                "budget": {"type": "number", "description": "Бюджет"},
                "concerns": {"type": "array", "items": {"type": "string", "enum": ["акне"]}},
            },
        }
    ),
    ScenarioConfig.model_validate(
        {
            "key": "gift",
            "description": "Подарок",
            "instructions": "Выясни, кому подарок.",
            "slots": {
                "budget": {"type": "number", "description": "Бюджет, RUB"},
                "occasion": {"type": "string"},
                "is_surprise": {"type": "boolean"},
            },
        }
    ),
    ScenarioConfig.model_validate(
        {
            "key": "fragrance",
            "description": "Аромат",
            "instructions": "Выясни повод.",
            "slots": {"occasion": {"type": "string", "enum": ["office", "evening"]}},
        }
    ),
]


async def execute(
    arguments: dict[str, Any], active_scenario: str | None = "skincare"
) -> ToolResult:
    registry = ToolRegistry([update_dialog_state_tool(SCENARIOS)])
    ctx = ToolContext(
        tenant_id=TenantId(uuid4()),
        conversation_id=uuid4(),
        turn_id=uuid4(),
        active_scenario=active_scenario,
    )
    [result] = await registry.execute_many(
        [ToolInvocation(id="call_1", name=UPDATE_DIALOG_STATE, arguments=arguments)], ctx
    )
    return result


def test_schema_has_slots_of_all_scenarios() -> None:
    slots = update_dialog_state_tool(SCENARIOS).parameters["properties"]["slots"]

    assert list(slots["properties"]) == [
        "skin_type",
        "budget",
        "concerns",
        "occasion",
        "is_surprise",
    ]
    # Определения, различающиеся только описанием, — один вариант (описание первого).
    assert slots["properties"]["budget"]["anyOf"] == [
        {"type": "number", "description": "Бюджет"},
        {"type": "null"},
    ]
    assert slots["additionalProperties"] is False


@pytest.mark.parametrize(
    ("scenario", "occasion"), [("gift", "день рождения"), ("fragrance", "office")]
)
async def test_same_slot_with_different_definitions_accepts_any(
    scenario: str, occasion: str
) -> None:
    # occasion в gift — свободная строка, в fragrance — enum: годится значение по любому.
    result = await execute({"slots": {"occasion": occasion}}, active_scenario=scenario)

    assert result.ok
    assert result.state_patch == {"slots": {"occasion": occasion}}


async def test_scenario_becomes_active_scenario() -> None:
    result = await execute({"scenario": "gift", "slots": {"budget": 3000}})

    assert result.ok
    assert result.state_patch == {"slots": {"budget": 3000}, "active_scenario": "gift"}
    reset = await execute({"scenario": None, "facts": ["просто спросить"]})
    assert reset.state_patch == {"facts": ["просто спросить"], "active_scenario": None}


async def test_slots_of_another_scenario_are_validation_error() -> None:
    # Активен skincare: occasion — слот gift и fragrance, не skincare (ADR-0026).
    result = await execute({"slots": {"skin_type": "сухая", "occasion": "день рождения"}})

    assert result.error is not None and result.error.code == "validation_error"
    assert "occasion" in result.error.message and "skin_type" in result.error.message
    assert result.state_patch is None


async def test_slots_are_checked_against_scenario_argument() -> None:
    # Смена сценария и слоты нового в одном вызове; слоты прежнего — уже чужие.
    switched = await execute({"scenario": "gift", "slots": {"occasion": "день рождения"}})
    stale = await execute({"scenario": "gift", "slots": {"skin_type": "сухая"}})

    assert switched.ok
    assert stale.error is not None and stale.error.code == "validation_error"


@pytest.mark.parametrize(
    ("arguments", "active_scenario"),
    [
        ({"slots": {"budget": 3000}}, None),  # сценарий не выбран
        ({"scenario": None, "slots": {"budget": 3000}}, "gift"),  # сценарий снят
        ({"slots": {"budget": 3000}}, "removed"),  # сценария больше нет в конфиге
    ],
)
async def test_slots_without_scenario_are_validation_error(
    arguments: dict[str, Any], active_scenario: str | None
) -> None:
    result = await execute(arguments, active_scenario=active_scenario)

    assert result.error is not None and result.error.code == "validation_error"
    assert "scenario" in result.error.message
    # Без слотов сценарий не нужен: факты пишутся всегда.
    assert (await execute({"facts": ["спешит"]}, active_scenario=None)).ok


def test_scenario_argument_lists_scenario_keys() -> None:
    properties = update_dialog_state_tool(SCENARIOS).parameters["properties"]

    assert properties["scenario"]["anyOf"][0]["enum"] == ["skincare", "gift", "fragrance"]


def test_without_scenarios_there_is_no_scenario_argument() -> None:
    properties = update_dialog_state_tool([]).parameters["properties"]

    assert "scenario" not in properties


async def test_valid_arguments_become_state_patch() -> None:
    arguments = {
        "slots": {"skin_type": "сухая", "concerns": ["акне"], "budget": None},
        "facts": ["аллергия на отдушки"],
    }

    result = await execute(arguments)

    assert result.ok
    assert result.state_patch == arguments


@pytest.mark.parametrize(
    "arguments",
    [
        {"slots": {"hair_type": "сухие"}},  # слота нет ни в одном сценарии
        {"slots": {"skin_type": "нормальная"}},  # значение вне enum
        {"slots": {"concerns": ["морщины"]}},  # элемент массива вне enum
        {"slots": {"is_surprise": "да"}},  # неверный тип
        {"facts": [""]},
        {"scenario": "support"},  # сценария нет в конфиге
        {"active_scenario": "gift"},  # сценарий выбирается аргументом scenario
    ],
)
async def test_invalid_arguments_are_validation_errors(arguments: dict[str, Any]) -> None:
    result = await execute(arguments)

    assert result.error is not None and result.error.code == "validation_error"
    assert result.state_patch is None


def config(builtin: list[str]) -> AgentConfig:
    return AgentConfig.model_validate(
        {
            "assistant": {"name": "A", "greeting": "Привет", "fallback_message": "Ошибка"},
            "model": {"primary": {"provider": "openai", "name": "m"}},
            "limits": {},
            "prompt": {
                "tenant": "Ты — консультант.",
                "scenarios": [s.model_dump() for s in SCENARIOS],
            },
            "tools": {"builtin": builtin},
        }
    )


def test_builtin_tools_include_only_enabled_and_implemented() -> None:
    enabled = builtin_tools(config(["search_catalog", "update_dialog_state"]))

    assert [t.name for t in enabled] == [UPDATE_DIALOG_STATE]
    assert builtin_tools(config(["search_catalog"])) == []


# --- suggest_replies (P5-03a) ---


async def suggest(arguments: dict[str, Any]) -> ToolResult:
    registry = ToolRegistry([suggest_replies_tool()])
    ctx = ToolContext(tenant_id=TenantId(uuid4()), conversation_id=uuid4(), turn_id=uuid4())
    [result] = await registry.execute_many(
        [ToolInvocation(id="call_1", name=SUGGEST_REPLIES, arguments=arguments)], ctx
    )
    return result


async def test_suggestions_are_trimmed_in_order() -> None:
    result = await suggest({"options": [" Сухая ", "Жирная", "Не знаю"]})

    assert result.ok
    assert result.suggestions == ("Сухая", "Жирная", "Не знаю")
    assert result.state_patch is None and result.components == ()


async def test_blank_or_duplicate_after_trim_options_are_dropped() -> None:
    result = await suggest({"options": ["  ", "Да", "Да "]})

    assert result.suggestions == ("Да",)
    assert (await suggest({"options": [" "]})).suggestions == ()


@pytest.mark.parametrize(
    "arguments",
    [
        {},
        {"options": []},
        {"options": ["a", "b", "c", "d", "e"]},
        {"options": ["x" * 41]},
        {"options": ["Да", "Да"]},
        {"options": ["Да"], "extra": 1},
    ],
)
async def test_invalid_suggestions_are_validation_errors(arguments: dict[str, Any]) -> None:
    result = await suggest(arguments)

    assert result.error is not None and result.error.code == "validation_error"
    assert result.suggestions == ()


def test_suggest_replies_is_enabled_by_config() -> None:
    assert [t.name for t in builtin_tools(config(["suggest_replies"]))] == [SUGGEST_REPLIES]
