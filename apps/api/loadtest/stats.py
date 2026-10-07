"""Сводка прогона: перцентили латентностей, ошибки, пропускная способность."""

import math
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass


@dataclass(frozen=True)
class TurnSample:
    """Один ход посетителя. Время — секунды от отправки запроса хода; None — события не было."""

    started_s: float | None  # до `turn_started`
    first_token_s: float | None  # до первого `text_delta` (TTFT)
    duration_s: float  # до `done`, ошибки или обрыва
    outcome: str  # `completed` | `interrupted` | `failed` | `http_<код>` | `error_<код>` | ...


@dataclass(frozen=True)
class Latency:
    count: int
    p50: float
    p95: float
    p99: float
    max: float


@dataclass(frozen=True)
class Summary:
    turns: int
    completed: int
    outcomes: dict[str, int]
    error_rate: float
    turns_per_s: float
    started: Latency | None
    first_token: Latency | None
    duration: Latency | None
    create_conversation: Latency | None
    create_errors: dict[str, int]


def percentile(values: Sequence[float], q: float) -> float:
    """Перцентиль по ближайшему рангу (q в процентах): значение из выборки, без интерполяции."""
    if not values:
        raise ValueError("пустая выборка")
    ordered = sorted(values)
    rank = max(1, math.ceil(q / 100 * len(ordered)))
    return ordered[rank - 1]


def latency(values: Sequence[float]) -> Latency | None:
    if not values:
        return None
    return Latency(
        count=len(values),
        p50=percentile(values, 50),
        p95=percentile(values, 95),
        p99=percentile(values, 99),
        max=max(values),
    )


def summarize(
    turns: Sequence[TurnSample],
    create_conversation_s: Sequence[float],
    create_errors: Sequence[str],
    wall_s: float,
) -> Summary:
    """Латентности — только по завершённым ходам; ошибки — доля незавершённых."""
    completed = [t for t in turns if t.outcome == "completed"]
    return Summary(
        turns=len(turns),
        completed=len(completed),
        outcomes=dict(Counter(t.outcome for t in turns)),
        error_rate=(len(turns) - len(completed)) / len(turns) if turns else 0.0,
        turns_per_s=len(completed) / wall_s if wall_s > 0 else 0.0,
        started=latency([t.started_s for t in completed if t.started_s is not None]),
        first_token=latency([t.first_token_s for t in completed if t.first_token_s is not None]),
        duration=latency([t.duration_s for t in completed]),
        create_conversation=latency(create_conversation_s),
        create_errors=dict(Counter(create_errors)),
    )


def render(summary: Summary) -> str:
    lines = [
        f"ходов: {summary.turns}, завершено: {summary.completed}, "
        f"ошибок: {summary.error_rate:.1%}, ходов/с: {summary.turns_per_s:.1f}",
        "исходы: " + ", ".join(f"{k}={v}" for k, v in sorted(summary.outcomes.items())),
    ]
    if summary.create_errors:
        errors = ", ".join(f"{k}={v}" for k, v in sorted(summary.create_errors.items()))
        lines.append(f"диалог не создан: {errors}")
    lines.append(f"{'метрика, мс':<24}{'n':>6}{'p50':>8}{'p95':>8}{'p99':>8}{'max':>8}")
    rows = [
        ("создание диалога", summary.create_conversation),
        ("до turn_started", summary.started),
        ("до первого токена", summary.first_token),
        ("ход целиком", summary.duration),
    ]
    for name, value in rows:
        if value is None:
            lines.append(f"{name:<24}{0:>6}{'—':>8}{'—':>8}{'—':>8}{'—':>8}")
            continue
        ms = [round(v * 1000) for v in (value.p50, value.p95, value.p99, value.max)]
        lines.append(f"{name:<24}{value.count:>6}" + "".join(f"{v:>8}" for v in ms))
    return "\n".join(lines)
