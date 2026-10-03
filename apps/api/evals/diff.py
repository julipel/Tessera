"""`make eval-diff [a=... b=...]` → `python -m evals.diff [OLD NEW]`: что улучшилось и что
ухудшилось между двумя прогонами по их JSON-отчётам (evals/report.py).

Без аргументов сравниваются два последних отчёта в `evals/reports/`. Код выхода: 0 — ухудшений
нет, 1 — есть ухудшения, 2 — отчёт не найден или не читается.
"""

import argparse
import json
import sys
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

from evals.checks import CheckStatus
from evals.dialogs import REPO_ROOT
from evals.report import CHECK_ICON, REPORT_VERSION, REPORTS_DIR

_PASSED = CheckStatus.PASSED.value
_PROBLEMS = (CheckStatus.FAILED.value, CheckStatus.ERROR.value)


class InvalidReportError(Exception):
    pass


class Change(StrEnum):
    IMPROVED = "improved"
    REGRESSED = "regressed"
    CHANGED = "changed"
    ADDED = "added"
    REMOVED = "removed"


@dataclass(frozen=True, slots=True)
class StatusChange:
    """Смена статуса диалога (`turn` и `check` — None) или проверки хода. `old`/`new` — статус
    в старом/новом прогоне, None — диалога или проверки там нет; `detail` — из нового прогона
    (из старого, если проверка исчезла)."""

    kind: Change
    dialog_id: str
    old: str | None
    new: str | None
    turn: int | None = None
    check: str | None = None
    detail: str = ""


@dataclass(frozen=True, slots=True)
class Metric:
    label: str
    old: float
    new: float


@dataclass(frozen=True, slots=True)
class ReportDiff:
    old_name: str
    new_name: str
    warnings: list[str] = field(default_factory=list)
    metrics: list[Metric] = field(default_factory=list)
    changes: list[StatusChange] = field(default_factory=list)

    def of(self, kind: Change) -> list[StatusChange]:
        return [change for change in self.changes if change.kind is kind]

    @property
    def regressed(self) -> bool:
        return bool(self.of(Change.REGRESSED))


def load_report(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        raise InvalidReportError(f"{path}: {e}") from e
    if not isinstance(data, dict) or data.get("version") != REPORT_VERSION:
        version = data.get("version") if isinstance(data, dict) else None
        raise InvalidReportError(f"{path}: версия отчёта {version!r}, ожидается {REPORT_VERSION}")
    return data


def resolve_report(arg: str, reports: Path = REPORTS_DIR) -> Path:
    """Путь к отчёту, путь от корня репозитория или имя в `reports` (можно без `.json`)."""
    for candidate in (Path(arg), REPO_ROOT / arg, reports / arg, reports / f"{arg}.json"):
        if candidate.is_file():
            return candidate
    raise InvalidReportError(f"отчёт не найден: {arg}")


def latest_reports(reports: Path = REPORTS_DIR) -> tuple[Path, Path]:
    """Два последних отчёта: имена — таймстемпы, сортировка по имени = по времени."""
    found = sorted(reports.glob("*.json"))
    if len(found) < 2:
        raise InvalidReportError(f"в {reports} меньше двух JSON-отчётов — сравнивать нечего")
    return found[-2], found[-1]


def classify(old: str, new: str) -> Change:
    if old == _PASSED and new in _PROBLEMS:
        return Change.REGRESSED
    if old in _PROBLEMS and new == _PASSED:
        return Change.IMPROVED
    return Change.CHANGED


def diff_reports(
    old: dict[str, Any], new: dict[str, Any], old_name: str = "old", new_name: str = "new"
) -> ReportDiff:
    old_dialogs = {d["id"]: d for d in old["dialogs"]}
    new_dialogs = {d["id"]: d for d in new["dialogs"]}
    warnings = _condition_warnings(old, new)
    changes: list[StatusChange] = []
    for dialog_id in [*new_dialogs, *(d for d in old_dialogs if d not in new_dialogs)]:
        before, after = old_dialogs.get(dialog_id), new_dialogs.get(dialog_id)
        if before is None or after is None:
            present = after or before or {}
            changes.append(
                StatusChange(
                    Change.ADDED if before is None else Change.REMOVED,
                    dialog_id,
                    old=None if before is None else before["status"],
                    new=None if after is None else after["status"],
                    detail=present.get("error") or "",
                )
            )
            continue
        if _inputs(before) != _inputs(after):
            warnings.append(f"Диалог `{dialog_id}` изменён (реплики) — ходы могут не совпадать")
        check_changes = _check_changes(dialog_id, before, after)
        errors = _errors(after)
        # Смену статуса диалога объясняют изменившиеся проверки; отдельной строкой — только
        # когда проверки её не показывают: ошибка агента или конфига.
        if before["status"] != after["status"] and (not check_changes or _errors(before) != errors):
            changes.append(
                StatusChange(
                    classify(before["status"], after["status"]),
                    dialog_id,
                    before["status"],
                    after["status"],
                    detail="; ".join(errors),
                )
            )
        changes += check_changes
    return ReportDiff(
        old_name=old_name,
        new_name=new_name,
        warnings=warnings,
        metrics=_metrics(old, new),
        changes=changes,
    )


def _inputs(dialog: dict[str, Any]) -> list[Any]:
    return [turn["input"] for turn in dialog["turns"]]


def _errors(dialog: dict[str, Any]) -> list[str]:
    """Ошибки диалога (конфиг) и ходов (агент упал), по которым статус ⚠️."""
    errors = [dialog.get("error"), *(turn.get("error") for turn in dialog["turns"])]
    return [error for error in errors if error]


def _checks(dialog: dict[str, Any]) -> dict[tuple[int, str], dict[str, Any]]:
    return {
        (turn["index"], check["name"]): check
        for turn in dialog["turns"]
        for check in turn["checks"]
    }


def _check_changes(
    dialog_id: str, before: dict[str, Any], after: dict[str, Any]
) -> list[StatusChange]:
    old_checks, new_checks = _checks(before), _checks(after)
    changes: list[StatusChange] = []
    for key in sorted(old_checks.keys() | new_checks.keys()):
        old, new = old_checks.get(key), new_checks.get(key)
        if old is not None and new is not None and old["status"] == new["status"]:
            continue
        if old is None or new is None:
            kind = Change.ADDED if old is None else Change.REMOVED
        else:
            kind = classify(old["status"], new["status"])
        turn, name = key
        changes.append(
            StatusChange(
                kind,
                dialog_id,
                old=None if old is None else old["status"],
                new=None if new is None else new["status"],
                turn=turn,
                check=name,
                detail=(new or old or {}).get("detail", ""),
            )
        )
    return changes


_CONDITIONS = (
    ("judge_prompt_version", "Промпт судьи", "вердикты судьи напрямую несопоставимы"),
    ("platform_prompt_version", "Platform-промпт", ""),
    ("models", "Модели агента", ""),
    ("judges", "Судья", ""),
    ("filter", "Фильтр", "наборы диалогов могут различаться"),
)


def _condition_warnings(old: dict[str, Any], new: dict[str, Any]) -> list[str]:
    warnings = []
    for key, label, consequence in _CONDITIONS:
        if old.get(key) != new.get(key):
            note = f" — {consequence}" if consequence else ""
            warnings.append(
                f"{label}: {_condition(old.get(key))} → {_condition(new.get(key))}{note}"
            )
    return warnings


def _condition(value: Any) -> str:
    if isinstance(value, dict):
        return ", ".join(f"{k} — {v}" for k, v in value.items()) or "—"
    return "—" if value is None else f"`{value}`"


def _metrics(old: dict[str, Any], new: dict[str, Any]) -> list[Metric]:
    old_dialogs = Counter(d["status"] for d in old["dialogs"])
    new_dialogs = Counter(d["status"] for d in new["dialogs"])
    old_checks, new_checks = _check_counter(old), _check_counter(new)
    metrics = [Metric("Диалоги", len(old["dialogs"]), len(new["dialogs"]))]
    metrics += [
        Metric(f"Диалоги {CHECK_ICON[status]}", old_dialogs[status], new_dialogs[status])
        for status in CheckStatus
        if old_dialogs[status] or new_dialogs[status]
    ]
    metrics.append(Metric("Проверки", sum(old_checks.values()), sum(new_checks.values())))
    metrics += [
        Metric(f"Проверки {CHECK_ICON[status]}", old_checks[status], new_checks[status])
        for status in CheckStatus
        if old_checks[status] or new_checks[status]
    ]
    for key, label in (
        ("input_tokens", "Токены агента, вход"),
        ("output_tokens", "Токены агента, выход"),
        ("judge_input_tokens", "Токены судьи, вход"),
        ("judge_output_tokens", "Токены судьи, выход"),
    ):
        metrics.append(Metric(label, _turn_sum(old, key), _turn_sum(new, key)))
    metrics.append(Metric("Длительность, с", _duration(old), _duration(new)))
    return metrics


def _check_counter(report: dict[str, Any]) -> Counter[str]:
    return Counter(
        check["status"]
        for dialog in report["dialogs"]
        for turn in dialog["turns"]
        for check in turn["checks"]
    )


def _turn_sum(report: dict[str, Any], key: str) -> int:
    return sum(turn.get(key, 0) for dialog in report["dialogs"] for turn in dialog["turns"])


def _duration(report: dict[str, Any]) -> float:
    started = datetime.fromisoformat(report["started_at"])
    finished = datetime.fromisoformat(report["finished_at"])
    return round((finished - started).total_seconds())


_SECTIONS = (
    (Change.REGRESSED, "Ухудшилось"),
    (Change.IMPROVED, "Улучшилось"),
    (Change.CHANGED, "Изменилось"),
    (Change.ADDED, "Новое"),
    (Change.REMOVED, "Исчезло"),
)


def render_diff(diff: ReportDiff) -> str:
    lines = [f"# Сравнение эвалов: {diff.old_name} → {diff.new_name}", ""]
    if diff.warnings:
        lines += [f"- ⚠️ {warning}" for warning in diff.warnings]
        lines.append("")
    lines += ["| | Было | Стало | Δ |", "|---|---|---|---|"]
    lines += [
        f"| {m.label} | {m.old:g} | {m.new:g} | {_delta(m.new - m.old)} |" for m in diff.metrics
    ]
    if not diff.changes:
        lines += ["", "Статусы диалогов и проверок не изменились."]
    for kind, title in _SECTIONS:
        changes = diff.of(kind)
        if changes:
            lines += ["", f"## {title} ({len(changes)})", ""]
            lines += [_render_change(change) for change in changes]
    return "\n".join(lines) + "\n"


def _render_change(change: StatusChange) -> str:
    where = f"`{change.dialog_id}`"
    if change.check is None:
        where = f"диалог {where}"
    else:
        where += f" ход {change.turn} `{change.check}`"
    detail = f" — {change.detail}" if change.detail else ""
    return f"- {where}: {_icon(change.old)} → {_icon(change.new)}{detail}"


def _icon(status: str | None) -> str:
    return "—" if status is None else CHECK_ICON[CheckStatus(status)]


def _delta(value: float) -> str:
    return "" if value == 0 else f"{value:+g}"


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="evals.diff", description="Сравнение двух прогонов")
    parser.add_argument(
        "reports_to_compare",
        nargs="*",
        metavar="REPORT",
        help="старый и новый отчёт: путь или имя в --reports (по умолчанию — два последних)",
    )
    parser.add_argument("--reports", type=Path, default=REPORTS_DIR, help="каталог отчётов")
    args = parser.parse_args(argv)
    try:
        match args.reports_to_compare:
            case []:
                old_path, new_path = latest_reports(args.reports)
            case [old_arg, new_arg]:
                old_path = resolve_report(old_arg, args.reports)
                new_path = resolve_report(new_arg, args.reports)
            case _:
                print("нужно два отчёта или ни одного", file=sys.stderr)
                return 2
        old, new = load_report(old_path), load_report(new_path)
    except InvalidReportError as e:
        print(e, file=sys.stderr)
        return 2
    diff = diff_reports(old, new, old_path.stem, new_path.stem)
    print(render_diff(diff), end="")
    return 1 if diff.regressed else 0


if __name__ == "__main__":
    sys.exit(main())
