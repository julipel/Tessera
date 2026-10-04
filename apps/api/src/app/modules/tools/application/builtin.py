"""Встроенные инструменты ядра (contracts.md §4), включаемые тенанту через `tools.builtin`.

Реализованы `update_dialog_state`, `search_knowledge`, `search_catalog` и `get_entity`;
остальные имена из конфига пропускаются — их добавят следующие задачи. Инструменты с
зависимостями подключаются, только если окружение их собрало: `search_knowledge` — поиск
по знаниям (`knowledge`), каталог — `catalog`; иначе — предупреждение в лог.
"""

from collections.abc import Callable, Iterable, Mapping
from typing import Any

import structlog

from app.contracts import AgentConfig, ScenarioConfig, SlotDefinition
from app.modules.tools.application.catalog import (
    GET_ENTITY,
    SEARCH_CATALOG,
    get_entity_tool,
    search_catalog_tool,
)
from app.modules.tools.application.knowledge import SEARCH_KNOWLEDGE, search_knowledge_tool
from app.modules.tools.domain.definition import ToolContext, ToolDefinition
from app.modules.tools.domain.ports import Catalog, KnowledgeSearcher
from app.modules.tools.domain.result import ToolResult

logger = structlog.get_logger(__name__)

UPDATE_DIALOG_STATE = "update_dialog_state"

_DESCRIPTION = (
    "Запиши, что пользователь сообщил о своём запросе: значения слотов сценария и важные "
    "факты вне слотов. Вызывай, как только узнал новое, — записанное попадает в состояние "
    "диалога, и переспрашивать его не нужно. Чтобы стереть слот, передай null."
)


def builtin_tools(
    config: AgentConfig,
    *,
    knowledge: KnowledgeSearcher | None = None,
    catalog: Catalog | None = None,
) -> list[ToolDefinition]:
    """Встроенные инструменты из `config.tools.builtin`, которые уже реализованы и для
    которых есть зависимости."""
    factories: dict[str, Callable[[AgentConfig], ToolDefinition]] = {
        UPDATE_DIALOG_STATE: lambda c: update_dialog_state_tool(c.prompt.scenarios or []),
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
    enabled = config.tools.builtin or []
    if knowledge is None and SEARCH_KNOWLEDGE in enabled:
        logger.warning("tools.search_knowledge_unavailable", reason="поиск по знаниям не настроен")
    if catalog is None and {SEARCH_CATALOG, GET_ENTITY} & set(enabled):
        logger.warning("tools.catalog_unavailable", reason="каталог не подключён")
    return [factories[name](config) for name in enabled if name in factories]


def update_dialog_state_tool(scenarios: Iterable[ScenarioConfig]) -> ToolDefinition:
    """Схема аргументов — по слотам всех сценариев: лишние слоты и неверные типы отклоняет
    реестр (`validation_error`), модель исправляется. Слот с одним именем в разных сценариях
    берётся из первого."""
    slots: dict[str, Any] = {}
    for scenario in scenarios:
        for name, slot in (scenario.slots or {}).items():
            slots.setdefault(name, {"anyOf": [_slot_schema(slot), {"type": "null"}]})
    return ToolDefinition(
        name=UPDATE_DIALOG_STATE,
        description=_DESCRIPTION,
        parameters={
            "type": "object",
            "properties": {
                "slots": {"type": "object", "properties": slots, "additionalProperties": False},
                "facts": {
                    "type": "array",
                    "items": {"type": "string", "minLength": 1},
                    "description": "Короткие факты о пользователе и запросе.",
                },
            },
            "additionalProperties": False,
        },
        handler=_update_dialog_state,
        timeout_s=1,
        display_label="Запоминаю",
    )


async def _update_dialog_state(arguments: Mapping[str, Any], ctx: ToolContext, /) -> ToolResult:
    patch = {key: arguments[key] for key in ("slots", "facts") if key in arguments}
    return ToolResult(content="Записано в состояние диалога.", state_patch=patch)


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
