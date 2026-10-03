"""Состояние диалога (architecture.md §7): что агент уже знает о запросе пользователя.

Всегда передаётся модели целиком — поэтому агент не переспрашивает. Обновляется патчами из
`ToolResult.state_patch` (формат — contracts.md §4).
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True, slots=True)
class DialogState:
    """`slots` — значения слотов сценариев; `facts` — что пользователь сообщил вне слотов;
    `shown_entities` — id уже показанных сущностей; `active_scenario` — ключ сценария."""

    slots: Mapping[str, Any] = field(default_factory=dict)
    facts: tuple[str, ...] = ()
    shown_entities: tuple[str, ...] = ()
    active_scenario: str | None = None

    def apply(self, patch: Mapping[str, Any]) -> "DialogState":
        """Новое состояние с патчем: слоты сливаются (`null` удаляет слот), факты и показанные
        сущности дописываются без повторов, `active_scenario` заменяется. Неизвестные ключи
        патча игнорируются."""
        slots = dict(self.slots)
        for name, value in (patch.get("slots") or {}).items():
            if value is None:
                slots.pop(name, None)
            else:
                slots[name] = value
        return DialogState(
            slots=slots,
            facts=_append_unique(self.facts, patch.get("facts")),
            shown_entities=_append_unique(self.shown_entities, patch.get("shown_entities")),
            active_scenario=patch.get("active_scenario", self.active_scenario),
        )

    @property
    def is_empty(self) -> bool:
        return self == DialogState()

    def to_dict(self) -> dict[str, Any]:
        """JSON для хранения и для Runtime-слоя промпта; пустые части опускаются."""
        data: dict[str, Any] = {}
        if self.slots:
            data["slots"] = dict(self.slots)
        if self.facts:
            data["facts"] = list(self.facts)
        if self.shown_entities:
            data["shown_entities"] = list(self.shown_entities)
        if self.active_scenario is not None:
            data["active_scenario"] = self.active_scenario
        return data

    @classmethod
    def from_dict(cls, data: Mapping[str, Any] | None) -> "DialogState":
        """Из сохранённого JSON; пустой или неполный — недостающие части пустые."""
        return cls().apply(data or {})


def _append_unique(current: tuple[str, ...], new: Sequence[Any] | None) -> tuple[str, ...]:
    result = list(current)
    for item in new or ():
        if isinstance(item, str) and item not in result:
            result.append(item)
    return tuple(result)
