"""Ошибки модуля tenants."""

from app.modules.shared.kernel import DomainError, NotFoundError


class AgentConfigNotFoundError(NotFoundError):
    """Версия конфигурации не найдена в пределах тенанта."""


class InvalidTenantSpecError(DomainError):
    """Описание тенанта (YAML) не прошло валидацию."""


class InvalidWidgetKeyError(DomainError):
    """Ключ виджета не передан, неизвестен или принадлежит отключённому тенанту."""


class NoActiveConfigError(NotFoundError):
    """Нет активной версии AgentConfig."""
