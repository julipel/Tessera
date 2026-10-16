"""Ошибки модуля access."""

from app.modules.shared.kernel import DomainError


class InvalidEmailError(DomainError):
    """Email пользователя админки не похож на адрес."""


class WeakPasswordError(DomainError):
    """Пароль короче минимальной или длиннее максимальной длины."""


class PasswordRequiredError(DomainError):
    """Новому пользователю нужен пароль."""


class InvalidCredentialsError(DomainError):
    """Неверный email или пароль, либо пользователь отключён — причину не раскрываем."""
