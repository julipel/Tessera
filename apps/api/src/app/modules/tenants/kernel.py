"""Язык диалога без фреймворков — для других модулей, включая agent (ADR-0025).

`public.py` тянет FastAPI и SQLAlchemy, а выбор языка нужен и chat при создании диалога,
и агентному циклу при сборке хода.
"""

from app.modules.tenants.domain.language import (
    AssistantTexts,
    Language,
    assistant_texts,
    resolve_language,
)

__all__ = ["AssistantTexts", "Language", "assistant_texts", "resolve_language"]
