"""Ошибки модуля tenants."""

from app.modules.shared.kernel import DomainError, NotFoundError


class AgentConfigNotFoundError(NotFoundError):
    """Версия конфигурации не найдена в пределах тенанта."""


class InvalidTenantSpecError(DomainError):
    """Описание тенанта (YAML) не прошло валидацию."""
