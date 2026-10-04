"""Секреты источников (ADR-0017): конфиг хранит имя переменной окружения, не значение.

Имя обязано начинаться с `SOURCE_SECRET_`: иначе конфиг тенанта мог бы сослаться на секрет
платформы (`OPENAI_API_KEY`, `DATABASE_URL`) и отправить его на свой сервер.
"""

import re
from collections.abc import Mapping

SECRET_PREFIX = "SOURCE_SECRET_"
_NAME = re.compile(rf"{SECRET_PREFIX}[A-Z0-9_]+")


class SourceSecretError(Exception):
    """Имя секрета не разрешено или переменная не задана."""


class SourceSecrets:
    def __init__(self, env: Mapping[str, str]) -> None:
        self.env = env

    def get(self, name: str) -> str:
        if not _NAME.fullmatch(name):
            raise SourceSecretError(
                f"секрет {name!r}: разрешены только переменные {SECRET_PREFIX}<A-Z0-9_>"
            )
        value = self.env.get(name)
        if not value:
            raise SourceSecretError(f"секрет {name}: переменная окружения не задана")
        return value
