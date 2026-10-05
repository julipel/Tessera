"""Типы memory без фреймворков — для domain-слоёв других модулей (ADR-0008)."""

from app.modules.memory.domain.dialog_state import DialogState, PendingConfirmation

__all__ = ["DialogState", "PendingConfirmation"]
