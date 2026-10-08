"""Ошибки модуля tenants."""

from collections.abc import Sequence
from dataclasses import dataclass

from app.modules.shared.kernel import DomainError, NotFoundError


class AgentConfigNotFoundError(NotFoundError):
    """Версия конфигурации не найдена в пределах тенанта."""


class InvalidTenantSpecError(DomainError):
    """Описание тенанта (YAML) не прошло валидацию."""


class InvalidWidgetKeyError(DomainError):
    """Ключ виджета не передан, неизвестен или принадлежит отключённому тенанту."""


class OriginNotAllowedError(DomainError):
    """Запрос с ключом пришёл со страницы сайта, которого нет в allowed_origins ключа."""


class NoActiveConfigError(NotFoundError):
    """Нет активной версии AgentConfig."""


@dataclass(frozen=True, slots=True)
class ConfigProblem:
    """Ошибка в одном месте AgentConfig: `loc` — путь (ключи и индексы), пустой — весь конфиг."""

    loc: tuple[str | int, ...]
    message: str


class InvalidAgentConfigError(DomainError):
    """AgentConfig не разбирается как YAML или не проходит валидацию."""

    def __init__(self, problems: Sequence[ConfigProblem]) -> None:
        super().__init__("; ".join(_describe(p) for p in problems))
        self.problems = tuple(problems)


def _describe(problem: ConfigProblem) -> str:
    path = ".".join(str(part) for part in problem.loc)
    return f"{path}: {problem.message}" if path else problem.message
