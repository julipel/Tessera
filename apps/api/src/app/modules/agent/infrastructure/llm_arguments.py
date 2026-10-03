"""Общее для LLM-адаптеров: разбор аргументов вызова инструмента из строки JSON."""

import json
from typing import Any


def parse_arguments(raw: str) -> dict[str, Any] | None:
    """Пустая строка — инструмент без аргументов; не JSON-объект — None (цикл вернёт ошибку)."""
    if not raw.strip():
        return {}
    try:
        value = json.loads(raw)
    except ValueError:
        return None
    return value if isinstance(value, dict) else None
