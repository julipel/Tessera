"""Пароли и email пользователей админки (ADR-0036).

Пароль хэшируется scrypt из stdlib (argon2/bcrypt в зависимостях нет). Параметры хранятся
в самом хэше: их можно усилить без миграции — старые хэши проверяются со своими.
"""

import base64
import hashlib
import hmac
import re
import secrets

from app.modules.access.domain.errors import InvalidEmailError, WeakPasswordError

MIN_PASSWORD_LENGTH = 12
_SCHEME = "scrypt"
# N=2^15, r=8 — 32 МиБ памяти и ~0,1 с CPU на проверку: перебор по утёкшему дампу дорог.
_N, _R, _P = 2**15, 8, 1
_SALT_BYTES = 16
_KEY_BYTES = 32
_MAXMEM = 64 * 1024 * 1024  # по умолчанию OpenSSL — 32 МиБ, впритык к 128·r·N

_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def normalize_email(email: str) -> str:
    """Email — логин: без пробелов по краям и без учёта регистра."""
    normalized = email.strip().lower()
    if len(normalized) > 320 or not _EMAIL.fullmatch(normalized):
        raise InvalidEmailError(f"некорректный email: {email!r}")
    return normalized


def check_password_strength(password: str) -> None:
    if len(password) < MIN_PASSWORD_LENGTH:
        raise WeakPasswordError(f"пароль короче {MIN_PASSWORD_LENGTH} символов")


def hash_password(password: str) -> str:
    """`scrypt$N$r$p$соль$хэш` (base64). Медленно — из async-кода через to_thread."""
    salt = secrets.token_bytes(_SALT_BYTES)
    key = _scrypt(password, salt, _N, _R, _P)
    return "$".join([_SCHEME, str(_N), str(_R), str(_P), _b64(salt), _b64(key)])


def verify_password(password: str, encoded: str) -> bool:
    """Неизвестный формат хэша — False, а не исключение."""
    try:
        scheme, n, r, p, salt, key = encoded.split("$")
        if scheme != _SCHEME:
            return False
        expected = base64.b64decode(key)
        actual = _scrypt(password, base64.b64decode(salt), int(n), int(r), int(p))
    except ValueError:
        return False
    return hmac.compare_digest(actual, expected)


def _scrypt(password: str, salt: bytes, n: int, r: int, p: int) -> bytes:
    return hashlib.scrypt(
        password.encode(), salt=salt, n=n, r=r, p=p, maxmem=_MAXMEM, dklen=_KEY_BYTES
    )


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode()
