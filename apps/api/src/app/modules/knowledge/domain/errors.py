"""Ошибки модуля knowledge."""

from collections.abc import Sequence
from dataclasses import dataclass

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


@dataclass(frozen=True, slots=True)
class SourceProblem:
    """Ошибка в месте ввода источника: `loc` — путь из ключей и индексов, пустой — весь ввод."""

    loc: tuple[str | int, ...]
    message: str


class InvalidSourceError(DomainError, ValueError):
    """Источник не прошёл проверку: имя, вид, YAML или конфиг коннектора. ValueError —
    для seed, который сообщает о неверном конфиге текстом."""

    def __init__(self, message: str, problems: Sequence[SourceProblem] = ()) -> None:
        super().__init__(message)
        self.problems = list(problems) or [SourceProblem((), message)]


class SourceFileTooLargeError(DomainError):
    """Загружаемый файл больше лимита."""
