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


class CatalogError(DomainError):
    """Каталог недоступен: сбой запроса к БД."""


class InvalidSourceDeclarationError(DomainError):
    """Декларация источников тенанта (YAML, ADR-0019) невалидна: имя, вид, конфиг, файлы."""
