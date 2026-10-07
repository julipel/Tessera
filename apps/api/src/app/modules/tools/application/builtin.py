"""Встроенные инструменты ядра (contracts.md §4), включаемые тенанту через `tools.builtin`.

Реализованы `update_dialog_state`, `search_knowledge`, `search_catalog`, `get_entity`,
`show_entities`, `suggest_replies`, `show_form` и `create_lead`;
остальные имена из конфига пропускаются — их добавят следующие задачи. Инструменты с
зависимостями подключаются, только если окружение их собрало: `search_knowledge` — поиск
по знаниям (`knowledge`), каталог — `catalog`, `create_lead` — хранилище заявок (`leads`);
иначе — предупреждение в лог. `show_form` и `create_lead` без `forms` в конфиге
не подключаются.
"""

from collections.abc import Callable, Iterable, Mapping, Sequence
from typing import Any

import structlog

from app.contracts import AgentConfig, ScenarioConfig, SlotDefinition
from app.modules.tools.application.catalog import (
    GET_ENTITY,
    SEARCH_CATALOG,
    get_entity_tool,
    search_catalog_tool,
)
from app.modules.tools.application.forms import (
    CREATE_LEAD,
    SHOW_FORM,
    create_lead_tool,
    show_form_tool,
)
from app.modules.tools.application.knowledge import SEARCH_KNOWLEDGE, search_knowledge_tool
from app.modules.tools.application.labels import localize_display_labels
from app.modules.tools.application.show_entities import SHOW_ENTITIES, show_entities_tool
from app.modules.tools.application.suggest_replies import SUGGEST_REPLIES, suggest_replies_tool
from app.modules.tools.domain.definition import ToolContext, ToolDefinition, ToolHandler
from app.modules.tools.domain.ports import Catalog, KnowledgeSearcher, LeadStore
from app.modules.tools.domain.result import ToolError, ToolResult

logger = structlog.get_logger(__name__)

UPDATE_DIALOG_STATE = "update_dialog_state"

_DESCRIPTION = (
    "Запиши, что пользователь сообщил о своём запросе: значения слотов сценария и важные "
    "факты вне слотов. Вызывай, как только узнал новое, — записанное попадает в состояние "
    "диалога, и переспрашивать его не нужно. Слоты — только активного сценария или того, "
    "что передаёшь в scenario. Чтобы стереть слот, передай null. Вызывай "
    "в одном шаге с другими инструментами (поиском, показом), а не отдельным шагом."
)
_SCENARIO_DESCRIPTION = (
    "Сценарий диалога: отметь, как только понял задачу клиента, и смени, если задача "
    "сменилась. null — задача не подходит ни под один сценарий."
)


def builtin_tools(
    config: AgentConfig,
    *,
    knowledge: KnowledgeSearcher | None = None,
    catalog: Catalog | None = None,
    leads: LeadStore | None = None,
    language: str = "ru",
) -> list[ToolDefinition]:
    """Встроенные инструменты из `config.tools.builtin`, которые уже реализованы и для
    которых есть зависимости; подписи tool_started и строки платформы в компонентах — на
    языке диалога `language`. Тексты тенанта переводит вызывающий (`localize_config`)."""
    factories: dict[str, Callable[[AgentConfig], ToolDefinition]] = {
        UPDATE_DIALOG_STATE: lambda c: update_dialog_state_tool(c.prompt.scenarios or []),
        SUGGEST_REPLIES: lambda c: suggest_replies_tool(),
    }
    if knowledge is not None:
        factories[SEARCH_KNOWLEDGE] = lambda c: search_knowledge_tool(
            knowledge, c.knowledge.search_knowledge if c.knowledge else None
        )
    if catalog is not None:
        factories[SEARCH_CATALOG] = lambda c: search_catalog_tool(
            catalog, c.knowledge.catalog if c.knowledge else None
        )
        factories[GET_ENTITY] = lambda c: get_entity_tool(catalog)
        factories[SHOW_ENTITIES] = lambda c: show_entities_tool(
            catalog, c.knowledge.catalog if c.knowledge else None, language
        )
    forms = config.forms or {}
    if forms:
        factories[SHOW_FORM] = lambda c: show_form_tool(forms)
        if leads is not None:
            factories[CREATE_LEAD] = lambda c: create_lead_tool(forms, leads)
    enabled = config.tools.builtin or []
    if not forms and {SHOW_FORM, CREATE_LEAD} & set(enabled):
        logger.warning("tools.forms_unavailable", reason="в конфиге нет forms")
    if forms and leads is None and CREATE_LEAD in enabled:
        logger.warning("tools.create_lead_unavailable", reason="хранилище заявок не подключено")
    if knowledge is None and SEARCH_KNOWLEDGE in enabled:
        logger.warning("tools.search_knowledge_unavailable", reason="поиск по знаниям не настроен")
    if catalog is None and {SEARCH_CATALOG, GET_ENTITY, SHOW_ENTITIES} & set(enabled):
        logger.warning("tools.catalog_unavailable", reason="каталог не подключён")
    tools = [factories[name](config) for name in enabled if name in factories]
    return localize_display_labels(tools, language)


def update_dialog_state_tool(scenarios: Iterable[ScenarioConfig]) -> ToolDefinition:
    """Схема аргументов — по слотам всех сценариев: так в одном вызове можно сменить
    сценарий и записать слоты нового. Неверные типы отклоняет реестр, слоты не из сценария
    вызова (`scenario`, без него — активного) — обработчик (`validation_error`, ADR-0026),
    модель исправляется. Слот с одним именем и разными определениями в разных сценариях
    принимает значение по любому из них. `scenario` — ключ сценария, есть в схеме, только
    если сценарии заданы; в состояние он попадает как `active_scenario` и в промпте действует
    со следующего хода."""
    scenarios = list(scenarios)
    variants: dict[str, list[dict[str, Any]]] = {}
    for scenario in scenarios:
        for name, slot in (scenario.slots or {}).items():
            schemas = variants.setdefault(name, [])
            schema = _slot_schema(slot)
            if not any(_same_slot(schema, known) for known in schemas):
                schemas.append(schema)
    slots = {name: {"anyOf": [*schemas, {"type": "null"}]} for name, schemas in variants.items()}
    properties: dict[str, Any] = {
        "slots": {"type": "object", "properties": slots, "additionalProperties": False},
        "facts": {
            "type": "array",
            "items": {"type": "string", "minLength": 1},
            "description": "Короткие факты о пользователе и запросе.",
        },
    }
    if scenarios:
        properties["scenario"] = {
            "anyOf": [{"type": "string", "enum": [s.key for s in scenarios]}, {"type": "null"}],
            "description": _SCENARIO_DESCRIPTION,
        }
    return ToolDefinition(
        name=UPDATE_DIALOG_STATE,
        description=_DESCRIPTION,
        parameters={"type": "object", "properties": properties, "additionalProperties": False},
        handler=_dialog_state_handler(scenarios),
        timeout_s=1,
        display_label="Запоминаю",
    )


def _dialog_state_handler(scenarios: Sequence[ScenarioConfig]) -> ToolHandler:
    declared = {s.key: list(s.slots or {}) for s in scenarios}

    async def update_dialog_state(arguments: Mapping[str, Any], ctx: ToolContext, /) -> ToolResult:
        # Слоты — сценария вызова: аргумент scenario, без него — активный (ADR-0026).
        scenario = arguments.get("scenario", ctx.active_scenario)
        if problem := _foreign_slots(arguments.get("slots") or {}, scenario, declared):
            return ToolResult(error=ToolError("validation_error", problem, retryable=False))
        patch = {key: arguments[key] for key in ("slots", "facts") if key in arguments}
        if "scenario" in arguments:
            patch["active_scenario"] = arguments["scenario"]
        return ToolResult(content="Записано в состояние диалога.", state_patch=patch)

    return update_dialog_state


def _foreign_slots(
    slots: Mapping[str, Any], scenario: str | None, declared: Mapping[str, list[str]]
) -> str | None:
    if not slots:
        return None
    if scenario is None or scenario not in declared:
        return (
            f"{UPDATE_DIALOG_STATE}: слоты записываются в сценарий — передай scenario вместе "
            "со слотами или запиши сведения в facts"
        )
    foreign = [name for name in slots if name not in declared[scenario]]
    if not foreign:
        return None
    allowed = ", ".join(declared[scenario]) or "нет"
    return (
        f"{UPDATE_DIALOG_STATE}: слотов {', '.join(foreign)} нет в сценарии {scenario} "
        f"(его слоты: {allowed}). Если задача клиента сменилась — передай scenario, иначе "
        "запиши сведения в facts"
    )


def _same_slot(a: Mapping[str, Any], b: Mapping[str, Any]) -> bool:
    # Описание на допустимые значения не влияет: «Бюджет, RUB» и «Бюджет» — один слот.
    return {k: v for k, v in a.items() if k != "description"} == {
        k: v for k, v in b.items() if k != "description"
    }


def _slot_schema(slot: SlotDefinition) -> dict[str, Any]:
    schema: dict[str, Any] = {"type": slot.type}
    if slot.description:
        schema["description"] = slot.description
    if slot.enum:
        schema["enum"] = list(slot.enum)
    if slot.type == "array" and slot.items is not None:
        items: dict[str, Any] = {"type": slot.items.type}
        if slot.items.enum:
            items["enum"] = list(slot.items.enum)
        schema["items"] = items
    return schema
