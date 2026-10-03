"""Сравнение прогонов эвалов (evals/diff.py) на отчётах, собранных `report.to_json`."""

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from evals.checks import CheckResult, CheckStatus
from evals.diff import (
    Change,
    InvalidReportError,
    diff_reports,
    latest_reports,
    load_report,
    main,
    render_diff,
    resolve_report,
)
from evals.report import RunInfo, to_json
from evals.runner import DialogResult, TurnResult

START = datetime(2026, 10, 3, 12, 0, tzinfo=UTC)

P, F, E, S = CheckStatus.PASSED, CheckStatus.FAILED, CheckStatus.ERROR, CheckStatus.SKIPPED


def turn(index: int, *checks: tuple[str, CheckStatus], text: str = "Привет") -> TurnResult:
    return TurnResult(
        index=index,
        input={"type": "text", "text": text},
        checks=[CheckResult(name, status, f"{name}: {status}") for name, status in checks],
        input_tokens=100,
        output_tokens=10,
    )


def dialog(dialog_id: str, *turns: TurnResult, error: str | None = None) -> DialogResult:
    return DialogResult(dialog_id, "eval-shop", [], list(turns), error)


def report(*dialogs: DialogResult, minutes: int = 1, **conditions: Any) -> dict[str, Any]:
    info = RunInfo(
        started_at=START,
        finished_at=START.replace(minute=minutes),
        platform_prompt_version=conditions.get("platform", "1"),
        judge_prompt_version=conditions.get("judge_prompt", "1"),
        models=conditions.get("models", {"eval-shop": "openai/a"}),
        judges=conditions.get("judges", {"eval-shop": "openai/j"}),
        filter=conditions.get("filter"),
    )
    return to_json(list(dialogs), info)


def test_check_transitions_are_classified() -> None:
    old = report(
        dialog("d1", turn(1, ("tools_called", P), ("judge", F)), turn(2, ("components", S)))
    )
    new = report(
        dialog("d1", turn(1, ("tools_called", F), ("judge", P)), turn(2, ("components", P)))
    )

    diff = diff_reports(old, new)

    assert [(c.dialog_id, c.turn, c.check) for c in diff.of(Change.REGRESSED)] == [
        ("d1", 1, "tools_called")
    ]
    assert diff.of(Change.REGRESSED)[0].detail == "tools_called: failed"
    assert [c.check for c in diff.of(Change.IMPROVED)] == ["judge"]
    # skipped → passed — не улучшение: проверка просто не выполнялась (судья выключен).
    assert [(c.check, c.old, c.new) for c in diff.of(Change.CHANGED)] == [
        ("components", "skipped", "passed")
    ]
    assert diff.regressed
    assert not diff.warnings


def test_failed_to_error_is_change_and_unchanged_is_absent() -> None:
    old = report(dialog("d1", turn(1, ("judge", F), ("tools_called", P))))
    new = report(dialog("d1", turn(1, ("judge", E), ("tools_called", P))))

    diff = diff_reports(old, new)

    assert [(c.check, c.kind) for c in diff.changes] == [("judge", Change.CHANGED)]
    assert not diff.regressed


def test_dialog_status_change_without_check_changes_is_regression() -> None:
    # Агент упал на ходе: проверки хода не считались, но диалог стал ⚠️.
    old = report(dialog("d1", turn(1, ("tools_called", P))))
    new = report(dialog("d1", turn(1), error="конфиг не загружен"))

    diff = diff_reports(old, new)

    assert [(c.check, c.kind) for c in diff.changes] == [
        (None, Change.REGRESSED),
        ("tools_called", Change.REMOVED),
    ]
    assert diff.changes[0].detail == "конфиг не загружен"
    assert diff.regressed


def test_turn_error_is_dialog_regression() -> None:
    old = report(dialog("d1", turn(1, ("judge", P)), turn(2, ("judge", P))))
    crashed = turn(1)
    crashed.error = "LLMError: timeout"
    new = report(dialog("d1", crashed, TurnResult(index=2, input={}, skipped=True)))

    diff = diff_reports(old, new)

    assert [(c.turn, c.kind) for c in diff.changes] == [
        (None, Change.REGRESSED),
        (1, Change.REMOVED),
        (2, Change.REMOVED),
    ]
    assert diff.changes[0].detail == "LLMError: timeout"


def test_added_and_removed_dialogs_and_checks() -> None:
    old = report(dialog("d1", turn(1, ("judge", P))), dialog("gone", turn(1, ("judge", F))))
    new = report(dialog("d1", turn(1, ("judge", P), ("components", F))), dialog("fresh", turn(1)))

    diff = diff_reports(old, new)

    assert [(c.dialog_id, c.check, c.old, c.new) for c in diff.of(Change.ADDED)] == [
        ("d1", "components", None, "failed"),
        ("fresh", None, None, "passed"),
    ]
    assert [(c.dialog_id, c.old) for c in diff.of(Change.REMOVED)] == [("gone", "failed")]
    # Новый провал — не ухудшение: сравнивать не с чем.
    assert not diff.regressed


def test_warnings_on_different_conditions_and_edited_dialog() -> None:
    old = report(dialog("d1", turn(1, text="Привет")))
    new = report(
        dialog("d1", turn(1, text="Здравствуйте")),
        judge_prompt="2",
        platform="3",
        models={"eval-shop": "openai/b"},
        filter="beauty",
    )

    warnings = diff_reports(old, new).warnings

    assert warnings == [
        "Промпт судьи: `1` → `2` — вердикты судьи напрямую несопоставимы",
        "Platform-промпт: `1` → `3`",
        "Модели агента: eval-shop — openai/a → eval-shop — openai/b",
        "Фильтр: — → `beauty` — наборы диалогов могут различаться",
        "Диалог `d1` изменён (реплики) — ходы могут не совпадать",
    ]


def test_render_diff() -> None:
    old = report(dialog("d1", turn(1, ("tools_called", P), ("judge", F))), minutes=2)
    new = report(
        dialog("d1", turn(1, ("tools_called", F), ("judge", P)), turn(2)),
        minutes=1,
        judge_prompt="2",
    )

    text = render_diff(diff_reports(old, new, "20261003-120000", "20261003-130000"))

    assert text.startswith("# Сравнение эвалов: 20261003-120000 → 20261003-130000\n")
    assert "- ⚠️ Промпт судьи: `1` → `2`" in text
    assert "| Проверки ✅ | 1 | 1 |  |" in text
    assert "| Токены агента, вход | 100 | 200 | +100 |" in text
    assert "| Длительность, с | 120 | 60 | -60 |" in text
    assert "## Ухудшилось (1)\n\n- `d1` ход 1 `tools_called`: ✅ → ❌ — tools_called: failed" in (
        text
    )
    assert "## Улучшилось (1)\n\n- `d1` ход 1 `judge`: ❌ → ✅" in text
    assert "Изменилось" not in text


def test_render_without_changes() -> None:
    same = report(dialog("d1", turn(1, ("judge", P))))
    assert "Статусы диалогов и проверок не изменились." in render_diff(diff_reports(same, same))


def write(directory: Path, name: str, data: dict[str, Any]) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{name}.json"
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return path


def test_latest_reports_and_resolve(tmp_path: Path) -> None:
    data = report(dialog("d1", turn(1)))
    for name in ("20261003-120000", "20261003-130000", "20261002-235959"):
        write(tmp_path, name, data)
    (tmp_path / "20261003-140000.md").write_text("не JSON", encoding="utf-8")

    old, new = latest_reports(tmp_path)

    assert (old.stem, new.stem) == ("20261003-120000", "20261003-130000")
    assert resolve_report("20261003-120000", tmp_path) == old
    assert resolve_report(str(new), tmp_path) == new
    with pytest.raises(InvalidReportError, match="не найден"):
        resolve_report("20990101-000000", tmp_path)


def test_latest_reports_needs_two(tmp_path: Path) -> None:
    write(tmp_path, "20261003-120000", report())
    with pytest.raises(InvalidReportError, match="меньше двух"):
        latest_reports(tmp_path)


def test_load_report_rejects_other_version_and_garbage(tmp_path: Path) -> None:
    other = write(tmp_path, "old", {**report(), "version": 99})
    garbage = tmp_path / "garbage.json"
    garbage.write_text("{", encoding="utf-8")

    with pytest.raises(InvalidReportError, match="версия отчёта 99"):
        load_report(other)
    with pytest.raises(InvalidReportError):
        load_report(garbage)


def test_main_exit_codes(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    good = write(tmp_path, "20261003-120000", report(dialog("d1", turn(1, ("judge", P)))))
    bad = write(tmp_path, "20261003-130000", report(dialog("d1", turn(1, ("judge", F)))))
    reports = ["--reports", str(tmp_path)]

    assert main(reports) == 1  # два последних: good → bad
    assert "## Ухудшилось (1)" in capsys.readouterr().out
    assert main([str(bad), str(good), *reports]) == 0
    assert "## Улучшилось (1)" in capsys.readouterr().out
    assert main(["20261003-120000", *reports]) == 2
    assert main(["20261003-120000", "nope", *reports]) == 2
    assert "отчёт не найден: nope" in capsys.readouterr().err
