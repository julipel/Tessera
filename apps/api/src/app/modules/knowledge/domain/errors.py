"""Ошибки модуля knowledge."""

from app.modules.shared.kernel import DomainError


class NoConnectorError(DomainError):
    """Для вида источника не зарегистрирован коннектор."""


class EmbeddingError(DomainError):
    """Не удалось получить эмбеддинги (провайдер недоступен, неверный ответ)."""


class VectorIndexError(DomainError):
    """Ошибка векторного индекса: Qdrant недоступен, отклонил запрос или не та коллекция."""


class RerankError(DomainError):
    """Реранкер недоступен или вернул неверный ответ."""
