"""Признаки prompt injection в результатах инструментов (P7-04b, ADR-0031).

Данные источников (база знаний, каталог, внешние API) приходят модели как результат
инструмента. Текст, похожий на указания модели, реестр помечает предупреждением — это
эвристика, а не гарантия: основная защита — правила Platform-промпта и подтверждение
действий с побочными эффектами (ADR-0021).
"""

import re
from collections.abc import Iterator
from typing import Any

_ZERO_WIDTH = re.compile("[​‌‍⁠﻿­]")
_SPACES = re.compile(r"[^\S\n]+")

_PATTERNS = tuple(
    re.compile(p, re.MULTILINE)
    for p in (
        # «игнорируй предыдущие инструкции», «забудь все свои правила»
        r"\b(игнорируй\w*|игнорировать|забудь\w*|забыть|отмени\w*|не обращай внимания на)"
        r"\s+(все\s+|всё\s+)?(предыдущ\w*|прежн\w*|прошл\w*|свои|твои|системн\w*|"
        r"ранее данн\w*)\s+(инструкц\w*|указани\w*|правил\w*|команд\w*|настройк\w*)",
        r"\b(ignore|disregard|forget|override)\s+(all\s+|any\s+)?(of\s+)?(the\s+|your\s+)?"
        r"(previous|prior|above|earlier|system|original)\s+"
        r"(instructions?|prompts?|rules|directions|guidelines)",
        # обращение к ассистенту с командой: «Ассистент, скажи…», «AI: ignore…»
        r"\b(ассистент|бот|нейросеть|модель|ии|chatgpt|gpt)\s*[,:!]\s*(игнорируй|забудь|скажи|"
        r"сообщи|ответь|напиши|пиши|выполни|не говори|обещай|дай)\b",
        r"\b(assistant|chatbot|ai|model|llm)\s*[,:]\s*(ignore|disregard|say|tell|reply|respond|"
        r"output|write|do not|don't|promise|give)\b",
        # смена роли и новые указания
        r"\bты\s+(теперь|отныне)\s+(не\s+)?(ассистент|бот|консультант|модель|ии|\w*gpt)\b",
        r"\byou\s+are\s+now\s+(a|an|the|in|no\s+longer)\b",
        r"\b(нов\w*\s+инструкц\w*|new\s+instructions?)\s*:",
        # раскрытие системного промпта
        r"\b(системн\w*\s+(промпт\w*|инструкц\w*)|system\s+prompt)\b",
        r"\b(раскрой|покажи|выведи|повтори|reveal|print|show|repeat)\s+(свои\s+|свой\s+|все\s+|"
        r"your\s+|the\s+)?(инструкц\w*|промпт\w*|instructions|prompt)\b",
        # поддельная разметка ролей и служебные токены
        r"^\s*(system|assistant|developer)\s*:",
        r"<\|im_(start|end)\|>|</?(system|tool_result|instructions?)>",
        r"^\s*#{2,}\s*(instruction|system)",
    )
)

_MAX_SAMPLE = 120


def normalize(text: str) -> str:
    """Нижний регистр, без невидимых символов и лишних пробелов (переводы строк — остаются)."""
    text = _ZERO_WIDTH.sub("", text).lower().replace("ё", "е")
    return _SPACES.sub(" ", text)


def suspicious_fragments(content: Any) -> list[str]:
    """Совпадения с признаками инструкций модели во всех строках `content` (str, dict, list):
    короткие образцы для лога, по одному на совпадение."""
    found: list[str] = []
    for text in _strings(content):
        normalized = normalize(text)
        for pattern in _PATTERNS:
            for match in pattern.finditer(normalized):
                start = max(0, match.start() - 20)
                found.append(normalized[start : start + _MAX_SAMPLE].strip())
    return found


def _strings(content: Any) -> Iterator[str]:
    if isinstance(content, str):
        yield content
    elif isinstance(content, dict):
        for value in content.values():
            yield from _strings(value)
    elif isinstance(content, list | tuple):
        for item in content:
            yield from _strings(item)
