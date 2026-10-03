"""Отчёт прогона: `evals/reports/<timestamp>.md` для чтения, `.json` рядом — для сравнения
прогонов (P3-02), и короткая сводка для консоли."""

import json
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from evals.checks import CheckStatus
from evals.dialogs import REPO_ROOT, describe_input
from evals.runner import DialogResult, RunStatus, TurnResult

REPORTS_DIR = REPO_ROOT / "evals" / "reports"
REPORT_VERSION = 1

_STATUS_ICON = {
    RunStatus.PASSED: "✅",
    RunStatus.FAILED: "❌",
    RunStatus.ERROR: "⚠️",
    RunStatus.SKIPPED: "⏭",
}
_CHECK_ICON = {
    CheckStatus.PASSED: "✅",
    CheckStatus.FAILED: "❌",
    CheckStatus.ERROR: "⚠️",
    CheckStatus.SKIPPED: "⏭",
}
_PROBLEMS = (CheckStatus.FAILED, CheckStatus.ERROR)


@dataclass(frozen=True, slots=True)
class RunInfo:
    """Условия прогона: `models` — модель агента по тенанту (`provider/name`), `judges` — модель
    судьи по тенанту (пусто — судья выключен)."""

    started_at: datetime
    finished_at: datetime
    platform_prompt_version: str
    judge_prompt_version: str
    models: dict[str, str] = field(default_factory=dict)
    judges: dict[str, str] = field(default_factory=dict)
    filter: str | None = None


@dataclass(frozen=True, slots=True)
class Totals:
    dialogs: Counter[RunStatus]
    checks: Counter[CheckStatus]
    input_tokens: int
    output_tokens: int
    judge_input_tokens: int
    judge_output_tokens: int

    @property
    def ok(self) -> bool:
        return not (self.dialogs[RunStatus.FAILED] or self.dialogs[RunStatus.ERROR])


def totals(results: Sequence[DialogResult]) -> Totals:
    turns = [turn for result in results for turn in result.turns]
    return Totals(
        dialogs=Counter(result.status for result in results),
        checks=Counter(check.status for result in results for check in result.checks),
        input_tokens=sum(turn.input_tokens for turn in turns),
        output_tokens=sum(turn.output_tokens for turn in turns),
        judge_input_tokens=sum(turn.judge_input_tokens for turn in turns),
        judge_output_tokens=sum(turn.judge_output_tokens for turn in turns),
    )


def write_report(
    results: Sequence[DialogResult], info: RunInfo, directory: Path = REPORTS_DIR
) -> Path:
    """Записать `<timestamp>.md` и `<timestamp>.json`; вернуть путь к Markdown."""
    directory.mkdir(parents=True, exist_ok=True)
    stem = info.started_at.strftime("%Y%m%d-%H%M%S")
    markdown = directory / f"{stem}.md"
    markdown.write_text(render_markdown(results, info), encoding="utf-8")
    (directory / f"{stem}.json").write_text(
        json.dumps(to_json(results, info), ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return markdown


def render_markdown(results: Sequence[DialogResult], info: RunInfo) -> str:
    total = totals(results)
    lines = [
        f"# Эвалы {info.started_at:%Y-%m-%d %H:%M:%S %Z}",
        "",
        f"- Диалоги: {_dialog_counts(total)}",
        f"- Проверки: {_check_counts(total)}",
        f"- Модели: {', '.join(f'{t} — {m}' for t, m in info.models.items()) or '—'}",
        f"- Судья: {', '.join(f'{t} — {m}' for t, m in info.judges.items()) or 'выключен'}"
        f" (промпт v{info.judge_prompt_version})",
        f"- Platform-промпт: v{info.platform_prompt_version}",
        f"- Токены агента: вход {total.input_tokens}, выход {total.output_tokens}",
        f"- Токены судьи: вход {total.judge_input_tokens}, выход {total.judge_output_tokens}",
        f"- Длительность: {(info.finished_at - info.started_at).total_seconds():.0f} с",
    ]
    if info.filter:
        lines.append(f"- Фильтр: `{info.filter}`")
    lines += ["", "| Диалог | Статус | Проверки | Провалы |", "|---|---|---|---|"]
    for result in results:
        checks = result.checks
        passed = sum(c.status is CheckStatus.PASSED for c in checks)
        failed = list(dict.fromkeys(c.name for c in checks if c.status in _PROBLEMS))
        lines.append(
            f"| [{result.dialog_id}](#{result.dialog_id}) | {_STATUS_ICON[result.status]} "
            f"| {passed}/{len(checks)} | {', '.join(failed) or result.error or ''} |"
        )
    for result in results:
        lines += ["", *_render_dialog(result)]
    return "\n".join(lines) + "\n"


def render_summary(results: Sequence[DialogResult], report: Path) -> str:
    total = totals(results)
    lines = []
    for result in results:
        checks = result.checks
        passed = sum(c.status is CheckStatus.PASSED for c in checks)
        lines.append(f"{_STATUS_ICON[result.status]} {result.dialog_id}  {passed}/{len(checks)}")
    lines += [
        "",
        f"Диалоги: {_dialog_counts(total)}",
        f"Проверки: {_check_counts(total)}",
        f"Отчёт: {report}",
    ]
    return "\n".join(lines)


def to_json(results: Sequence[DialogResult], info: RunInfo) -> dict[str, Any]:
    return {
        "version": REPORT_VERSION,
        "started_at": info.started_at.isoformat(),
        "finished_at": info.finished_at.isoformat(),
        "platform_prompt_version": info.platform_prompt_version,
        "judge_prompt_version": info.judge_prompt_version,
        "models": info.models,
        "judges": info.judges,
        "filter": info.filter,
        "dialogs": [
            {
                "id": result.dialog_id,
                "tenant": result.tenant,
                "tags": result.tags,
                "status": result.status.value,
                "error": result.error,
                "turns": [_turn_json(turn) for turn in result.turns],
            }
            for result in results
        ],
    }


def _turn_json(turn: TurnResult) -> dict[str, Any]:
    return {
        "index": turn.index,
        "input": turn.input,
        "status": turn.status.value,
        "error": turn.error,
        "finish": turn.finish,
        "text": turn.outcome.text,
        "tools_called": list(turn.outcome.tools_called),
        "components": list(turn.outcome.components),
        "slots": turn.outcome.slots,
        "input_tokens": turn.input_tokens,
        "output_tokens": turn.output_tokens,
        "judge_input_tokens": turn.judge_input_tokens,
        "judge_output_tokens": turn.judge_output_tokens,
        "duration_ms": turn.duration_ms,
        "checks": [
            {"name": c.name, "status": c.status.value, "detail": c.detail} for c in turn.checks
        ],
    }


def _render_dialog(result: DialogResult) -> list[str]:
    lines = [f"## {result.dialog_id}", ""]
    lines.append(
        f"{_STATUS_ICON[result.status]} {result.status.value} · тенант `{result.tenant}`"
        + (f" · теги: {', '.join(result.tags)}" if result.tags else "")
    )
    if result.error:
        lines += ["", f"**Ошибка:** {result.error}"]
    for turn in result.turns:
        lines += ["", *_render_turn(turn)]
    return lines


def _render_turn(turn: TurnResult) -> list[str]:
    lines = [f"### Ход {turn.index} {_STATUS_ICON[turn.status]}", ""]
    lines.append(f"**Пользователь:** {_input_label(turn.input)}")
    if turn.skipped:
        return [*lines, "", "_Не проигрывался: ошибка на предыдущем ходе._"]
    if turn.error:
        lines += ["", f"**Ошибка:** {turn.error}"]
    outcome = turn.outcome
    lines += ["", "**Ответ:**", "", _quote(outcome.text or "—"), ""]
    lines.append(
        f"Инструменты: {', '.join(outcome.tools_called) or '—'} · "
        f"компоненты: {', '.join(outcome.components) or '—'} · "
        f"слоты: `{json.dumps(outcome.slots, ensure_ascii=False)}` · "
        f"{turn.finish or '—'}, {turn.duration_ms} мс"
    )
    if turn.checks:
        lines.append("")
        for check in turn.checks:
            detail = f" — {check.detail}" if check.detail else ""
            lines.append(f"- {_CHECK_ICON[check.status]} `{check.name}`{detail}")
    return lines


def _input_label(user_input: dict[str, Any]) -> str:
    label = describe_input(user_input)
    return label if user_input.get("type") == "text" else f"`{label}`"


def _quote(text: str) -> str:
    return "\n".join(f"> {line}" if line else ">" for line in text.splitlines())


def _dialog_counts(total: Totals) -> str:
    count = sum(total.dialogs.values())
    parts = [f"{_STATUS_ICON[s]} {total.dialogs[s]}" for s in RunStatus if total.dialogs[s]]
    return f"{count}" + (f" · {' · '.join(parts)}" if parts else "")


def _check_counts(total: Totals) -> str:
    count = sum(total.checks.values())
    parts = [f"{_CHECK_ICON[s]} {total.checks[s]}" for s in CheckStatus if total.checks[s]]
    return f"{count}" + (f" · {' · '.join(parts)}" if parts else "")
