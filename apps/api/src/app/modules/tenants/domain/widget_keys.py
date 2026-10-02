"""Ключи виджета: в БД хранится только хэш (ADR-0007)."""

import hashlib
import secrets

KEY_PREFIX = "wk_"


def generate_widget_key() -> str:
    return KEY_PREFIX + secrets.token_urlsafe(32)


def hash_widget_key(key: str) -> str:
    # Ключ — случайный токен с высокой энтропией, медленный KDF не нужен.
    return hashlib.sha256(key.encode()).hexdigest()
