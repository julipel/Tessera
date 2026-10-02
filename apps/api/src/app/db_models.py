"""Реестр ORM-моделей для Alembic autogenerate.

Каждый модуль с таблицами добавляет сюда импорт своих моделей (через public.py),
чтобы они попали в `Base.metadata`.
"""

from app.modules.shared.public import Base

metadata = Base.metadata

__all__ = ["metadata"]
