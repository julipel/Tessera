"""Ошибки модуля knowledge."""

from app.modules.shared.kernel import DomainError


class NoConnectorError(DomainError):
    """Для вида источника не зарегистрирован коннектор."""
