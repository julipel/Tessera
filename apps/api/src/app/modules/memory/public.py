"""Публичный интерфейс модуля memory — единственная точка входа для других модулей."""

from app.modules.memory.domain.dialog_state import DialogState, PendingConfirmation

__all__ = ["DialogState", "PendingConfirmation"]
