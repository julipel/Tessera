"""Ключи виджета: в БД хранится только хэш (ADR-0007); сайты, где ключ разрешён (ADR-0022)."""

import hashlib
import re
import secrets
from collections.abc import Collection

KEY_PREFIX = "wk_"


def generate_widget_key() -> str:
    return KEY_PREFIX + secrets.token_urlsafe(32)


def hash_widget_key(key: str) -> str:
    # Ключ — случайный токен с высокой энтропией, медленный KDF не нужен.
    return hashlib.sha256(key.encode()).hexdigest()


# Origin в том виде, в каком его шлёт браузер: схема и хост в нижнем регистре, без пути и
# завершающего `/`. Тот же формат попадает в CSP `frame-ancestors`, поэтому без `*`, `;` и
# пробелов. Порт по умолчанию браузер не пишет — указывать его здесь бессмысленно.
ORIGIN_PATTERN = r"^https?://[a-z0-9]([a-z0-9.-]*[a-z0-9])?(:[0-9]{1,5})?$"
_ORIGIN = re.compile(ORIGIN_PATTERN)


def is_valid_origin(origin: str) -> bool:
    return bool(_ORIGIN.fullmatch(origin))


def origin_allowed(
    origin: str | None, allowed_origins: Collection[str], platform_origins: Collection[str]
) -> bool:
    """Можно ли обращаться с ключом со страницы `origin`.

    Без `Origin` (серверный клиент) — да: подделать заголовок вне браузера можно всё равно,
    от таких клиентов защищает rate limiting (P7-04). Свой веб-чат (iframe виджета) разрешён
    всегда — сайт тенанта ограничивает CSP `frame-ancestors` на странице iframe.
    """
    if origin is None:
        return True
    return origin in allowed_origins or origin in platform_origins
