"""Сводка ранней истории (architecture.md §7): когда сворачивать и какую часть.

Политика без LLM: старая часть истории сворачивается, когда история после прошлой сводки
длиннее порога; последние ходы остаются целиком. Граница — всегда начало хода (сообщение
пользователя), чтобы ответ не отрывался от вопроса.
"""

from collections.abc import Sequence
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class HistoryItem:
    from_user: bool
    text: str


def estimate_tokens(text: str) -> int:
    """Приблизительно: ~3 символа на токен (кириллица и латиница у токенизаторов OpenAI
    в среднем); токенизатора в проекте нет."""
    return (len(text) + 2) // 3


def fold_point(
    items: Sequence[HistoryItem], threshold_tokens: int, keep_recent_turns: int
) -> int | None:
    """Сколько первых элементов свернуть в сводку; None — сворачивать не нужно: история
    не длиннее порога или в ней не больше `keep_recent_turns` ходов."""
    if sum(estimate_tokens(item.text) for item in items) <= threshold_tokens:
        return None
    turn_starts = [i for i, item in enumerate(items) if item.from_user]
    if len(turn_starts) <= keep_recent_turns:
        return None
    point = turn_starts[-keep_recent_turns]
    return point or None
