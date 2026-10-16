"""Состояние диалога (architecture.md §7): что агент уже знает о запросе пользователя.

Передаётся модели (без слотов неактивных сценариев) — поэтому агент не переспрашивает.
Обновляется патчами из `ToolResult.state_patch` (формат — contracts.md §4). Слоты хранятся
по сценариям (ADR-0026).

Хранимый JSON несёт версию схемы `v` (REVIEW.md: M-3): без неё — формат до P6-05 (слоты
могут быть плоскими). Разбор терпимый: повреждённая часть отбрасывается и попадает в список
проблем, а не роняет ход — `from_dict` вызывается в начале каждого хода.

Состояние неизменяемо: словари слотов и аргументов копируются в read-only `MappingProxyType`
(значения внутри — как пришли из JSON). Хэша у состояния нет — в словари и множества его
не кладут.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from types import MappingProxyType
from typing import Any, ClassVar

# Больше фактов в состоянии не держим: самые старые отбрасываются (Runtime-слой промпта
# не растёт без конца, ADR-0039).
MAX_FACTS = 20
# Показанных сущностей — тоже не больше (три карусели по 8): модели нужны недавние, а каждый
# id — ~15 токенов на каждом ходе (дополнение к ADR-0039).
MAX_SHOWN_ENTITIES = 24
# Срок жизни ожидающего подтверждения (дополнение к ADR-0021): проигнорированная кнопка
# не исполнит устаревшие аргументы через несколько дней.
CONFIRMATION_TTL = timedelta(hours=1)
_CLOCK_SKEW = timedelta(minutes=1)
# Версия схемы хранимого JSON: 2 — слоты всегда по сценариям (P6-05); без `v` — 1.
STATE_VERSION = 2


@dataclass(frozen=True, slots=True)
class PendingConfirmation:
    """Вызов инструмента с `requires_confirmation`, ждущий ответа пользователя (ADR-0021):
    исполняется с этими аргументами, когда пользователь подтвердит `confirm_id`, если с
    `created_at` (UTC) прошло меньше `CONFIRMATION_TTL`. Без `created_at` — создано до срока
    жизни (P9-01ad) и считается просроченным."""

    __hash__: ClassVar[None] = None  # type: ignore[assignment]

    confirm_id: str
    tool: str
    arguments: Mapping[str, Any]
    created_at: datetime | None = None

    def __post_init__(self) -> None:
        if self.created_at is not None and self.created_at.tzinfo is None:
            raise ValueError("created_at без часового пояса")
        object.__setattr__(self, "arguments", MappingProxyType(dict(self.arguments)))

    def expired(self, now: datetime) -> bool:
        """Старше срока, без времени создания или из будущего (больше минуты — часы
        сбиты, срок не продлевается)."""
        if self.created_at is None:
            return True
        age = now - self.created_at
        return age >= CONFIRMATION_TTL or age < -_CLOCK_SKEW

    def to_dict(self) -> dict[str, Any]:
        data = {"confirm_id": self.confirm_id, "tool": self.tool, "arguments": dict(self.arguments)}
        if self.created_at is not None:
            data["created_at"] = self.created_at.isoformat()
        return data

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "PendingConfirmation":
        """Из JSON; `created_at` — строка ISO 8601 с часовым поясом, иначе `ValueError`."""
        return cls(
            confirm_id=data["confirm_id"],
            tool=data["tool"],
            arguments=data.get("arguments") or {},
            created_at=_timestamp(data.get("created_at")),
        )


@dataclass(frozen=True, slots=True)
class DialogState:
    """`slots` — значения слотов по сценариям (`{сценарий: {слот: значение}}`, ADR-0026);
    `facts` — что пользователь сообщил вне слотов (не больше `MAX_FACTS`); `shown_entities` —
    id уже показанных сущностей (не больше `MAX_SHOWN_ENTITIES`, последние показанные —
    в конце); `active_scenario` — ключ сценария; `pending_confirmation` —
    вызов, ждущий подтверждения пользователя."""

    slots: Mapping[str, Mapping[str, Any]] = field(default_factory=dict)
    facts: tuple[str, ...] = ()
    shown_entities: tuple[str, ...] = ()
    active_scenario: str | None = None
    pending_confirmation: PendingConfirmation | None = None

    __hash__: ClassVar[None] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        frozen = {key: MappingProxyType(dict(values)) for key, values in self.slots.items()}
        object.__setattr__(self, "slots", MappingProxyType(frozen))

    def apply(self, patch: Mapping[str, Any]) -> "DialogState":
        """Новое состояние с патчем: слоты патча (плоские) сливаются со слотами сценария
        патча — `active_scenario` патча, если он есть, иначе текущего; без сценария слоты
        не записываются (`null` удаляет слот). Факты из `forget_facts` (точный текст)
        убираются, затем факты дописываются без повторов; сверх `MAX_FACTS` отбрасываются
        самые старые (ADR-0039). Показанные сущности переносятся в конец (повторно
        показанная — снова свежая), сверх `MAX_SHOWN_ENTITIES` самые старые отбрасываются.
        `active_scenario` и `pending_confirmation` заменяются (`null` в `pending_confirmation`
        снимает ожидание). Неизвестные ключи патча игнорируются."""
        active = patch.get("active_scenario", self.active_scenario)
        return DialogState(
            slots=_merge_slots(self.slots, active, patch.get("slots") or {}),
            facts=_append_unique(
                _without(self.facts, patch.get("forget_facts")), patch.get("facts")
            )[-MAX_FACTS:],
            shown_entities=_append_recent(self.shown_entities, patch.get("shown_entities")),
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
        """JSON для хранения; пустые части опускаются, у непустого — версия схемы `v`."""
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
        return {"v": STATE_VERSION, **data} if data else data

    def prompt_dict(self) -> dict[str, Any]:
        """JSON для Runtime-слоя промпта: плоские слоты только активного сценария, без
        ожидающего вызова — о нём модель узнаёт из результата инструмента."""
        data = replace(self, slots={}, pending_confirmation=None).to_dict()
        data.pop("v", None)
        if self.active_slots:
            data = {"slots": dict(self.active_slots), **data}
        return data

    @classmethod
    def from_dict(cls, data: Mapping[str, Any] | None) -> "DialogState":
        """Из сохранённого JSON; повреждённые части отбрасываются (см. `parse`)."""
        return cls.parse(data)[0]

    @classmethod
    def parse(cls, data: Any) -> tuple["DialogState", list[str]]:
        """Состояние из сохранённого JSON и имена отброшенных частей (без значений — в них
        ПДн). Пустой или неполный JSON — недостающие части пустые. Версия 1 (без `v`):
        плоские слоты относятся к `active_scenario`, без него отбрасываются; версия 2 —
        слоты только по сценариям."""
        if data is None:
            return cls(), []
        if not isinstance(data, Mapping):
            return cls(), ["state"]
        problems: list[str] = []
        version = data.get("v", 1)
        # type(...) is int: True == 1 и 2.0 == 2 версией не считаются.
        if type(version) is not int or version not in (1, STATE_VERSION):
            # Неизвестная версия читается как текущая и при записи станет ею: новая версия
            # схемы требует совместимого отката (старый код теряет её поля).
            problems.append("v")
            version = STATE_VERSION
        active = data.get("active_scenario")
        if active is not None and not isinstance(active, str):
            problems.append("active_scenario")
            active = None
        slots, legacy = _parse_slots(data.get("slots"), version, problems)
        state = cls(
            slots=slots,
            facts=_strings(data.get("facts"), "facts", problems)[-MAX_FACTS:],
            shown_entities=_strings(data.get("shown_entities"), "shown_entities", problems)[
                -MAX_SHOWN_ENTITIES:
            ],
            active_scenario=active,
            pending_confirmation=_parse_pending(data.get("pending_confirmation"), problems),
        )
        if legacy and active is None:
            # Плоские слоты без сценария не к чему отнести — отбрасываются.
            problems.extend(f"slots.{key}" for key in legacy)
            legacy = {}
        return (state.apply({"slots": legacy}) if legacy else state), problems


def _parse_slots(
    raw: Any, version: int, problems: list[str]
) -> tuple[dict[str, Mapping[str, Any]], dict[str, Any]]:
    """Слоты по сценариям и (у версии 1) плоские слоты для `active_scenario`."""
    if raw is None:
        return {}, {}
    if not isinstance(raw, Mapping):
        problems.append("slots")
        return {}, {}
    slots: dict[str, Mapping[str, Any]] = {}
    legacy: dict[str, Any] = {}
    for key, value in raw.items():
        if isinstance(value, Mapping):
            if value:
                slots[str(key)] = dict(value)
        elif version == 1:
            legacy[key] = value
        else:
            problems.append(f"slots.{key}")
    return slots, legacy


def _strings(raw: Any, name: str, problems: list[str]) -> tuple[str, ...]:
    """Список строк без повторов; не список или не строки — отбрасываются с пометкой."""
    if raw is None:
        return ()
    if not isinstance(raw, list):
        problems.append(name)
        return ()
    if not all(isinstance(item, str) for item in raw):
        problems.append(name)
    return _append_unique((), raw)


def _parse_pending(raw: Any, problems: list[str]) -> PendingConfirmation | None:
    """Ожидание без `confirm_id`/`tool` строками, с `arguments` не словарём или с `created_at`
    не временем ISO 8601 снимается: нажатие кнопки получит ответ «подтверждение устарело»
    (ADR-0041)."""
    if raw is None:
        return None
    valid = (
        isinstance(raw, Mapping)
        and isinstance(raw.get("confirm_id"), str)
        and isinstance(raw.get("tool"), str)
        and (
            "arguments" not in raw
            or raw["arguments"] is None
            or isinstance(raw["arguments"], Mapping)
        )
    )
    if not valid:
        problems.append("pending_confirmation")
        return None
    try:
        return PendingConfirmation.from_dict(raw)
    except ValueError:  # created_at — не время ISO 8601 с поясом
        problems.append("pending_confirmation")
        return None


def _timestamp(raw: Any) -> datetime | None:
    if raw is None:
        return None
    if not isinstance(raw, str):
        raise ValueError("created_at — не строка")
    return datetime.fromisoformat(raw)


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


def _without(current: tuple[str, ...], forget: Sequence[Any] | None) -> tuple[str, ...]:
    if not forget:
        return current
    return tuple(item for item in current if item not in forget)


def _append_recent(current: tuple[str, ...], new: Sequence[Any] | None) -> tuple[str, ...]:
    added = _append_unique((), new)
    kept = tuple(item for item in current if item not in added)
    return (kept + added)[-MAX_SHOWN_ENTITIES:]


def _append_unique(current: tuple[str, ...], new: Sequence[Any] | None) -> tuple[str, ...]:
    result = list(current)
    for item in new or ():
        if isinstance(item, str) and item not in result:
            result.append(item)
    return tuple(result)
