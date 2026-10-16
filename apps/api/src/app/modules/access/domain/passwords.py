"""Пароли и email пользователей админки (ADR-0036).

Пароль хэшируется scrypt из stdlib (argon2/bcrypt в зависимостях нет). Параметры хранятся
в самом хэше: их можно усилить без миграции — старые хэши проверяются со своими.

Пароль приводится к NFKC: одинаково выглядящий пароль с другой раскладки или устройства
(составные и готовые символы, полноширинные цифры) совпадает. Хэши до этого (P9-01aa)
сделаны из строки как есть — для них проверка повторяется без нормализации.
"""

import base64
import hashlib
import hmac
import re
import secrets
import unicodedata

from app.modules.access.domain.errors import InvalidEmailError, WeakPasswordError

MIN_PASSWORD_LENGTH = 12
MAX_PASSWORD_LENGTH = 1024  # как у AdminLoginRequest.password
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
        # Без самого значения: сообщение попадает в логи и ответы (REVIEW.md: AC-13).
        raise InvalidEmailError("некорректный email")
    return normalized


def check_password_strength(password: str) -> None:
    password = _nfkc(password)  # длина того, что хэшируется
    if len(password) < MIN_PASSWORD_LENGTH:
        raise WeakPasswordError(f"пароль короче {MIN_PASSWORD_LENGTH} символов")
    if len(password) > MAX_PASSWORD_LENGTH:
        # Длиннее, чем принимает вход: с таким паролем было бы не войти.
        raise WeakPasswordError(f"пароль длиннее {MAX_PASSWORD_LENGTH} символов")


def hash_password(password: str) -> str:
    """`scrypt$N$r$p$соль$хэш` (base64). Медленно — из async-кода через to_thread."""
    salt = secrets.token_bytes(_SALT_BYTES)
    key = _scrypt(_nfkc(password), salt, _N, _R, _P)
    return "$".join([_SCHEME, str(_N), str(_R), str(_P), _b64(salt), _b64(key)])


def verify_password(password: str, encoded: str) -> bool:
    """Неизвестный формат хэша или недопустимые параметры (подменённый `n` в БД) — False,
    а не исключение и не 500."""
    try:
        scheme, n, r, p, salt, key = encoded.split("$")
        if scheme != _SCHEME:
            return False
        expected = base64.b64decode(key)
        params = (base64.b64decode(salt), int(n), int(r), int(p))
        if not _valid_params(*params[1:]):
            return False
        normalized = _nfkc(password)
        if hmac.compare_digest(_scrypt(normalized, *params), expected):
            return True
        # Хэш до NFKC: пароль с ненормализованными символами хэшировался как есть.
        return normalized != password and hmac.compare_digest(_scrypt(password, *params), expected)
    except ValueError:
        return False


def dummy_hash() -> str:
    """Хэш с текущими параметрами, которому не соответствует ни один пароль: для
    неизвестного email. Проверка по нему стоит столько же, сколько настоящая, а сам он
    вычисляется без KDF — первый неизвестный email не дороже остальных (REVIEW.md: AC-7)."""
    salt, key = secrets.token_bytes(_SALT_BYTES), secrets.token_bytes(_KEY_BYTES)
    return "$".join([_SCHEME, str(_N), str(_R), str(_P), _b64(salt), _b64(key)])


def _scrypt(password: str, salt: bytes, n: int, r: int, p: int) -> bytes:
    return hashlib.scrypt(
        password.encode(), salt=salt, n=n, r=r, p=p, maxmem=_MAXMEM, dklen=_KEY_BYTES
    )


def _valid_params(n: int, r: int, p: int) -> bool:
    """В пределах, которые scrypt принимает без TypeError/OverflowError (вне их stdlib бросает
    не ValueError). Превышение _MAXMEM внутри пределов — ValueError, он ловится."""
    return 1 < n <= 2**20 and n & (n - 1) == 0 and 1 <= r <= 32 and 1 <= p <= 16


def _nfkc(password: str) -> str:
    return unicodedata.normalize("NFKC", password)


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode()
