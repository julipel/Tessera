"""Сводка ранней истории (architecture.md §7): когда сворачивать и какую часть.

Политика без LLM: старая часть истории сворачивается, когда история после прошлой сводки
длиннее порога; последние ходы остаются целиком. Граница — всегда начало хода (сообщение
пользователя), чтобы ответ не отрывался от вопроса. Сворачивается не меньше половины порога
(гистерезис): если последние ходы сами длиннее порога, сводка не запускается на каждом ходе
ради одного свёрнутого сообщения.
"""

from collections.abc import Sequence
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class HistoryItem:
    from_user: bool
    text: str


def estimate_tokens(text: str) -> int:
    """Приблизительно, с запасом: ~2,5 символа на токен (кириллица у токенизаторов OpenAI
    и Anthropic — 2-2,5, латиница — ~4; порог лучше занизить, чем превысить); токенизатора
    в проекте нет."""
    return (len(text) * 2 + 4) // 5


def fold_point(
    items: Sequence[HistoryItem], threshold_tokens: int, keep_recent_turns: int
) -> int | None:
    """Сколько первых элементов свернуть в сводку; None — сворачивать не нужно: история
    не длиннее порога, в ней не больше `keep_recent_turns` ходов или старая часть короче
    половины порога. `keep_recent_turns` < 1 — `ValueError`."""
    if keep_recent_turns < 1:
        raise ValueError("keep_recent_turns должен быть >= 1")
    sizes = [estimate_tokens(item.text) for item in items]
    if sum(sizes) <= threshold_tokens:
        return None
    turn_starts = [i for i, item in enumerate(items) if item.from_user]
    if len(turn_starts) <= keep_recent_turns:
        return None
    # Ходов больше keep_recent_turns — граница не первый элемент, свёртка не пустая.
    point = turn_starts[-keep_recent_turns]
    if sum(sizes[:point]) * 2 < threshold_tokens:
        return None
    return point
