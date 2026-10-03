"""Проверки хода (evals/README.md, «Проверки»): детерминированные — по фактическим данным,
`clarifies`, `max_questions`, `judge` — по вердикту LLM-судьи (evals/judge.py).
"""

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from evals.dialogs import Expect


class CheckStatus(StrEnum):
    PASSED = "passed"
    FAILED = "failed"
    SKIPPED = "skipped"
    ERROR = "error"


@dataclass(frozen=True, slots=True)
class CheckResult:
    """`name` — ключ из `expect`; `detail` — что не так (для провала) или почему пропущено."""

    name: str
    status: CheckStatus
    detail: str = ""


@dataclass(frozen=True, slots=True)
class TurnOutcome:
    """Что агент сделал за ход: текст ответа, имена вызванных инструментов и типы
    UI-компонентов (в порядке появления), слоты DialogState после хода."""

    text: str = ""
    tools_called: tuple[str, ...] = ()
    components: tuple[str, ...] = ()
    slots: dict[str, Any] = field(default_factory=dict)


JUDGE_CHECKS = ("clarifies", "max_questions", "judge")


def needs_judge(expect: Expect) -> bool:
    return any(name in expect.model_fields_set for name in JUDGE_CHECKS)


def check_turn(
    expect: Expect, outcome: TurnOutcome, judged: Mapping[str, CheckResult] | None = None
) -> list[CheckResult]:
    """Проверки в порядке полей `Expect`; незаданные ожидания не проверяются. Проверки судьи
    берутся из `judged`; без вердикта (судья выключен) — `skipped`."""
    results: list[CheckResult] = []
    for name in Expect.model_fields:
        if name not in expect.model_fields_set:
            continue
        if name in JUDGE_CHECKS:
            skipped = CheckResult(name, CheckStatus.SKIPPED, "судья выключен")
            results.append((judged or {}).get(name, skipped))
            continue
        problems = _CHECKS[name](getattr(expect, name), outcome)
        status = CheckStatus.FAILED if problems else CheckStatus.PASSED
        results.append(CheckResult(name, status, "; ".join(problems)))
    return results


def _tools_called(expected: list[str], outcome: TurnOutcome) -> list[str]:
    missing = [name for name in expected if name not in outcome.tools_called]
    return [f"не вызваны: {', '.join(missing)}"] if missing else []


def _tools_not_called(expected: list[str], outcome: TurnOutcome) -> list[str]:
    called = [name for name in expected if name in outcome.tools_called]
    return [f"вызваны: {', '.join(called)}"] if called else []


def _components(expected: list[str], outcome: TurnOutcome) -> list[str]:
    missing = [kind for kind in expected if kind not in outcome.components]
    return [f"нет компонентов: {', '.join(missing)}"] if missing else []


def _state_contains(expected: dict[str, Any], outcome: TurnOutcome) -> list[str]:
    """Для списков — вхождение элементов, для остальных значений — равенство."""
    problems: list[str] = []
    for slot, value in expected.items():
        if slot not in outcome.slots:
            problems.append(f"нет слота {slot}")
            continue
        actual = outcome.slots[slot]
        if isinstance(value, list):
            ok = isinstance(actual, list) and all(item in actual for item in value)
        else:
            ok = actual == value
        if not ok:
            problems.append(f"{slot}: ожидалось {value!r}, есть {actual!r}")
    return problems


def _must_contain(expected: list[str], outcome: TurnOutcome) -> list[str]:
    text = outcome.text.casefold()
    missing = [s for s in expected if s.casefold() not in text]
    return [f"нет в ответе: {', '.join(map(repr, missing))}"] if missing else []


def _must_not_contain(expected: list[str], outcome: TurnOutcome) -> list[str]:
    text = outcome.text.casefold()
    found = [s for s in expected if s.casefold() in text]
    return [f"есть в ответе: {', '.join(map(repr, found))}"] if found else []


_CHECKS: dict[str, Callable[[Any, TurnOutcome], list[str]]] = {
    "tools_called": _tools_called,
    "tools_not_called": _tools_not_called,
    "components": _components,
    "state_contains": _state_contains,
    "must_contain": _must_contain,
    "must_not_contain": _must_not_contain,
}
