"""URL страниц коннектора `website`: нормализованный URL — это `external_id` документа.

Нормализация убирает различия, не меняющие страницу: регистр схемы и хоста, порт по
умолчанию, пустой путь, fragment. Путь и query не трогаются — их смысл определяет сайт.
"""

from urllib.parse import urljoin, urlsplit, urlunsplit

_DEFAULT_PORTS = {"http": 80, "https": 443}


def normalize_url(url: str, base: str | None = None) -> str | None:
    """Абсолютный нормализованный http(s)-URL или None (другая схема, логин в URL, мусор).

    Относительный `url` разрешается от `base`. IDN-хост переводится в punycode.
    """
    url = url.strip()
    if base is not None:
        url = urljoin(base, url)
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError:
        return None
    scheme = parts.scheme.lower()
    host = parts.hostname
    if scheme not in _DEFAULT_PORTS or not host or parts.username or parts.password:
        return None
    try:
        host = host.encode("idna").decode("ascii")
    except UnicodeError:
        return None
    if ":" in host:  # IPv6
        host = f"[{host}]"
    netloc = host if port in (None, _DEFAULT_PORTS[scheme]) else f"{host}:{port}"
    return urlunsplit((scheme, netloc, parts.path or "/", parts.query, ""))
