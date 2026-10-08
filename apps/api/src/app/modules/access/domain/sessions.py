"""Токены сессий админки (ADR-0036): в БД хранится только sha256 токена, как у ключей виджета."""

import hashlib
import secrets

TOKEN_PREFIX = "as_"


def generate_session_token() -> str:
    return TOKEN_PREFIX + secrets.token_urlsafe(32)  # 256 бит


def hash_session_token(token: str) -> str:
    # Токен — случайный, с высокой энтропией: медленный KDF не нужен.
    return hashlib.sha256(token.encode()).hexdigest()
