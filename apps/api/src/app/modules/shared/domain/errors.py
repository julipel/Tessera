"""Базовые доменные ошибки."""


class DomainError(Exception):
    """Базовая ошибка домена."""


class NotFoundError(DomainError):
    """Объект не найден в пределах тенанта."""


class TenantMismatchError(DomainError):
    """Попытка записать объект с tenant_id, отличным от тенанта операции."""
