"""Состояние диалога (architecture.md §7): что агент уже знает о запросе пользователя.

Передаётся модели (без слотов неактивных сценариев) — поэтому агент не переспрашивает.
Обновляется патчами из `ToolResult.state_patch` (формат — contracts.md §4). Слоты хранятся
по сценариям (ADR-0026).
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from typing import Any


@dataclass(frozen=True, slots=True)
class PendingConfirmation:
    """Вызов инструмента с `requires_confirmation`, ждущий ответа пользователя (ADR-0021):
    исполняется с этими аргументами, когда пользователь подтвердит `confirm_id`."""

    confirm_id: str
    tool: str
    arguments: Mapping[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {"confirm_id": self.confirm_id, "tool": self.tool, "arguments": dict(self.arguments)}

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "PendingConfirmation":
        return cls(
            confirm_id=data["confirm_id"], tool=data["tool"], arguments=data.get("arguments") or {}
        )


@dataclass(frozen=True, slots=True)
class DialogState:
    """`slots` — значения слотов по сценариям (`{сценарий: {слот: значение}}`, ADR-0026);
    `facts` — что пользователь сообщил вне слотов; `shown_entities` — id уже показанных
    сущностей; `active_scenario` — ключ сценария; `pending_confirmation` — вызов, ждущий
    подтверждения пользователя."""

    slots: Mapping[str, Mapping[str, Any]] = field(default_factory=dict)
    facts: tuple[str, ...] = ()
    shown_entities: tuple[str, ...] = ()
    active_scenario: str | None = None
    pending_confirmation: PendingConfirmation | None = None

    def apply(self, patch: Mapping[str, Any]) -> "DialogState":
        """Новое состояние с патчем: слоты патча (плоские) сливаются со слотами сценария
        патча — `active_scenario` патча, если он есть, иначе текущего; без сценария слоты
        не записываются (`null` удаляет слот). Факты и показанные сущности дописываются без
        повторов, `active_scenario` и `pending_confirmation` заменяются (`null`
        в `pending_confirmation` снимает ожидание). Неизвестные ключи патча игнорируются."""
        active = patch.get("active_scenario", self.active_scenario)
        return DialogState(
            slots=_merge_slots(self.slots, active, patch.get("slots") or {}),
            facts=_append_unique(self.facts, patch.get("facts")),
            shown_entities=_append_unique(self.shown_entities, patch.get("shown_entities")),
            active_scenario=active,
            pending_confirmation=_pending(patch, self.pending_confirmation),
        )

    @property
    def active_slots(self) -> Mapping[str, Any]:
        """Слоты активного сценария; без сценария — пусто."""
        if self.active_scenario is None:
            return {}
        return self.slots.get(self.active_scenario, {})

    @property
    def is_empty(self) -> bool:
        return self == DialogState()

    def to_dict(self) -> dict[str, Any]:
        """JSON для хранения; пустые части опускаются."""
        data: dict[str, Any] = {}
        if self.slots:
            data["slots"] = {scenario: dict(values) for scenario, values in self.slots.items()}
        if self.facts:
            data["facts"] = list(self.facts)
        if self.shown_entities:
            data["shown_entities"] = list(self.shown_entities)
        if self.active_scenario is not None:
            data["active_scenario"] = self.active_scenario
        if self.pending_confirmation is not None:
            data["pending_confirmation"] = self.pending_confirmation.to_dict()
        return data

    def prompt_dict(self) -> dict[str, Any]:
        """JSON для Runtime-слоя промпта: плоские слоты только активного сценария, без
        ожидающего вызова — о нём модель узнаёт из результата инструмента."""
        data = replace(self, slots={}, pending_confirmation=None).to_dict()
        if self.active_slots:
            data = {"slots": dict(self.active_slots), **data}
        return data

    @classmethod
    def from_dict(cls, data: Mapping[str, Any] | None) -> "DialogState":
        """Из сохранённого JSON; пустой или неполный — недостающие части пустые. Плоские
        слоты (сохранены до P6-05) относятся к `active_scenario`, без него отбрасываются."""
        data = data or {}
        state = cls().apply({k: v for k, v in data.items() if k != "slots"})
        slots: dict[str, Mapping[str, Any]] = {}
        legacy: dict[str, Any] = {}
        for key, value in (data.get("slots") or {}).items():
            if isinstance(value, Mapping):
                if value:
                    slots[key] = dict(value)
            else:
                legacy[key] = value
        state = replace(state, slots=slots)
        return state.apply({"slots": legacy}) if legacy else state


def _merge_slots(
    slots: Mapping[str, Mapping[str, Any]], scenario: str | None, patch: Mapping[str, Any]
) -> dict[str, Mapping[str, Any]]:
    merged = dict(slots)
    if scenario is None or not patch:
        return merged
    values = dict(merged.get(scenario, {}))
    for name, value in patch.items():
        if value is None:
            values.pop(name, None)
        else:
            values[name] = value
    if values:
        merged[scenario] = values
    else:
        merged.pop(scenario, None)
    return merged


def _pending(
    patch: Mapping[str, Any], current: PendingConfirmation | None
) -> PendingConfirmation | None:
    if "pending_confirmation" not in patch:
        return current
    value = patch["pending_confirmation"]
    return None if value is None else PendingConfirmation.from_dict(value)


def _append_unique(current: tuple[str, ...], new: Sequence[Any] | None) -> tuple[str, ...]:
    result = list(current)
    for item in new or ():
        if isinstance(item, str) and item not in result:
            result.append(item)
    return tuple(result)
