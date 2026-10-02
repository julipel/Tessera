"""Ошибки модуля chat."""

from app.modules.shared.kernel import NotFoundError


class ConversationNotFoundError(NotFoundError):
    """Диалога нет в пределах тенанта (или он принадлежит другому тенанту)."""


class NoActiveConfigError(NotFoundError):
    """Нет активного AgentConfig тенанта — диалог начать нельзя."""
