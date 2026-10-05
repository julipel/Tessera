"""Ошибки модуля chat."""

from app.modules.shared.kernel import DomainError, NotFoundError


class ConversationNotFoundError(NotFoundError):
    """Диалога нет в пределах тенанта (или он принадлежит другому тенанту)."""


class NoActiveConfigError(NotFoundError):
    """Нет активного AgentConfig тенанта — диалог начать нельзя."""


class DuplicateMessageError(DomainError):
    """Сообщение с таким client_message_id в диалоге уже есть — новый ход не запускается."""


class AgentConfigMissingError(DomainError):
    """Версии AgentConfig, с которой начат диалог, нет. Нарушение инварианта: FK с RESTRICT."""


class InvalidInputError(DomainError):
    """Ввод не подходит диалогу: например, отправлена форма, которой нет в конфиге, или её
    значения не проходят проверку полей."""
