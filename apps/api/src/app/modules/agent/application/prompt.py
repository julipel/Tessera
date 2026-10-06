"""Сборка системного промпта (architecture.md §6): слои Platform → Tenant → Scenario → Runtime.

Порядок слоёв фиксирован. Каждый слой обёрнут в тег — так слои отделены друг от друга,
а данные (состояние диалога) — от инструкций. Сборка детерминирована: одинаковый вход даёт
одинаковый текст.
"""

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from app.contracts import AgentConfig, ScenarioConfig, SlotDefinition
from app.modules.agent.application.platform_prompt import PLATFORM_PROMPT, PLATFORM_PROMPT_VERSION

_LANGUAGES = {"ru": "русском", "en": "английском", "sv": "шведском"}


@dataclass(frozen=True, slots=True)
class RuntimeContext:
    """Runtime-слой хода. `now` — с часовым поясом. `active_scenario` — ключ сценария из
    AgentConfig; None или неизвестный ключ — в промпт идут все сценарии. `language` — язык
    диалога (ADR-0025): при `assistant.language: auto` — язык ответа, когда по сообщению
    клиента его не определить."""

    now: datetime
    channel: str = "web"
    dialog_state: Mapping[str, Any] = field(default_factory=dict)
    active_scenario: str | None = None
    history_summary: str | None = None
    language: str | None = None


@dataclass(frozen=True, slots=True)
class SystemPrompt:
    text: str
    platform_version: str


def build_system_prompt(config: AgentConfig, runtime: RuntimeContext) -> SystemPrompt:
    layers = [
        _layer("platform", PLATFORM_PROMPT),
        _layer("tenant", config.prompt.tenant),
    ]
    scenarios = config.prompt.scenarios or []
    if scenarios:
        layers.append(_layer("scenarios", _scenarios(scenarios, runtime.active_scenario)))
    layers.append(_layer("runtime", _runtime(config, runtime)))
    return SystemPrompt(text="\n\n".join(layers), platform_version=PLATFORM_PROMPT_VERSION)


def _layer(tag: str, body: str) -> str:
    return f"<{tag}>\n{body.strip()}\n</{tag}>"


def _scenarios(scenarios: Sequence[ScenarioConfig], active_key: str | None) -> str:
    active = next((s for s in scenarios if s.key == active_key), None)
    if active is None:
        parts = [
            (
                "Сценарии диалога. Определи подходящий по запросу клиента и отметь его в "
                "состоянии диалога."
            )
        ]
        parts += [_scenario(s) for s in scenarios]
        return "\n\n".join(parts)
    parts = [f"Активный сценарий: {active.key}.", _scenario(active)]
    others = [s for s in scenarios if s is not active]
    if others:
        listing = "\n".join(f"- {s.key}: {s.description}" for s in others)
        parts.append(
            f"Если задача клиента сменилась, отметь в состоянии диалога другой сценарий:\n{listing}"
        )
    return "\n\n".join(parts)


def _scenario(scenario: ScenarioConfig) -> str:
    lines = [f"## {scenario.key} — {scenario.description}", scenario.instructions.strip()]
    if scenario.slots:
        lines.append("Слоты:")
        lines += [f"- {name} ({_slot_type(slot)})" for name, slot in scenario.slots.items()]
    return "\n".join(lines)


def _slot_type(slot: SlotDefinition) -> str:
    if slot.type == "array" and slot.items is not None:
        kind = f"array of {slot.items.type}"
        values = slot.items.enum
    else:
        kind = slot.type
        values = slot.enum
    text = f"{kind}: {' | '.join(values)}" if values else kind
    return f"{text}; {slot.description}" if slot.description else text


def _runtime(config: AgentConfig, runtime: RuntimeContext) -> str:
    state = _json_data(runtime.dialog_state) if runtime.dialog_state else "пока ничего не известно"
    lines = [
        f"Текущие дата и время: {runtime.now.isoformat(timespec='minutes')}",
        f"Канал: {runtime.channel}",
        _reply_language(config, runtime),
        f"Состояние диалога (уже известно, не переспрашивай):\n{state}",
    ]
    if runtime.history_summary:
        lines.append(f"Сводка ранней части диалога:\n{runtime.history_summary.strip()}")
    return "\n".join(lines)


def _reply_language(config: AgentConfig, runtime: RuntimeContext) -> str:
    fixed = config.assistant.language or "auto"
    if fixed in _LANGUAGES:
        return f"Отвечай на {_LANGUAGES[fixed]} языке."
    # Инструкции и данные обычно на языке тенанта и тянут ответ к нему — правило явное.
    text = (
        "Отвечай на языке последнего сообщения клиента, даже если инструкции, данные "
        "и прошлые ответы — на другом языке. Названия товаров и брендов не переводи."
    )
    if runtime.language in _LANGUAGES:
        text += (
            " Если по сообщению язык не определить (нажатие кнопки, отправка формы, короткий "
            f"ответ) — отвечай на {_LANGUAGES[runtime.language]} языке."
        )
    return text


def _json_data(data: Mapping[str, Any]) -> str:
    # `<` экранируется, чтобы значения от пользователя не могли закрыть тег слоя.
    text = json.dumps(data, ensure_ascii=False, indent=2, default=str)
    return text.replace("<", "\\u003c")
