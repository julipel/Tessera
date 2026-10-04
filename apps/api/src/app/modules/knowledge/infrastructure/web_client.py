"""HTTP-клиент краулера `website` (ADR-0013): GET с лимитами, ручными редиректами и SSRF-защитой.

Подключения ограничивает `GuardedTransport` (`web_guard`); клиент добавляет то, что видно
на уровне HTTP: только http(s) и порты по умолчанию, не больше `max_redirects` редиректов
и только на разрешённые хосты, ожидаемый Content-Type, лимит тела и общего времени запроса.
Тело распаковывается здесь же, с лимитом на распакованный размер (gzip-бомба).
"""

import asyncio
import zlib
from collections.abc import Collection, Mapping
from dataclasses import dataclass
from urllib.parse import urlsplit

import httpx

from app.modules.knowledge.infrastructure.web_guard import BlockedAddressError, GuardedTransport
from app.modules.knowledge.infrastructure.web_urls import normalize_url

DEFAULT_TIMEOUT_S = 15.0
DEFAULT_MAX_REDIRECTS = 5

_REDIRECTS = frozenset({301, 302, 303, 307, 308})
_DECODERS = {"gzip": 16 + zlib.MAX_WBITS, "x-gzip": 16 + zlib.MAX_WBITS, "deflate": zlib.MAX_WBITS}


class WebFetchError(Exception):
    """Запрос не выполнен: сеть, запрещённый адрес, редиректы, тип или размер ответа."""


class WebRejectedError(WebFetchError):
    """Адрес или ответ не подходят по правилам клиента (запрещённый адрес, редирект за пределы
    сайта, Content-Type, размер). В отличие от сбоев сети, повтор запроса не поможет."""


@dataclass(frozen=True, slots=True)
class WebResponse:
    """Ответ после редиректов. `url` — нормализованный адрес ответа; тело читается только
    у 2xx (у остальных пустое)."""

    url: str
    status: int
    content_type: str | None
    charset: str | None
    body: bytes
    etag: str | None = None
    last_modified: str | None = None
    truncated: bool = False


class WebClient:
    def __init__(
        self,
        http: httpx.AsyncClient,
        *,
        user_agent: str,
        timeout_s: float = DEFAULT_TIMEOUT_S,
        max_redirects: int = DEFAULT_MAX_REDIRECTS,
    ) -> None:
        self.http = http
        self.user_agent = user_agent
        self.timeout_s = timeout_s
        self.max_redirects = max_redirects

    async def get(
        self,
        url: str,
        *,
        max_bytes: int,
        accept: Collection[str] | None = None,
        allowed_hosts: Collection[str] | None = None,
        truncate: bool = False,
        headers: Mapping[str, str] | None = None,
    ) -> WebResponse:
        """GET `url`. `accept` — допустимые MIME-типы 2xx-ответа; `allowed_hosts` — хосты,
        на которые можно перейти по редиректу. Тело больше `max_bytes` — ошибка, а с
        `truncate` — обрезается (`truncated=True`). `headers` — дополнительные заголовки
        (User-Agent и Accept-Encoding задаёт клиент); с секретами в заголовках ограничивайте
        редиректы `allowed_hosts`."""
        try:
            async with asyncio.timeout(self.timeout_s):
                return await self._get(url, max_bytes, accept, allowed_hosts, truncate, headers)
        except TimeoutError as e:
            raise WebFetchError(f"{url}: нет ответа за {self.timeout_s} с") from e
        except BlockedAddressError as e:
            raise WebRejectedError(f"{url}: {e}") from e
        except httpx.HTTPError as e:
            raise WebFetchError(f"{url}: {type(e).__name__}: {e}") from e

    async def _get(
        self,
        url: str,
        max_bytes: int,
        accept: Collection[str] | None,
        allowed_hosts: Collection[str] | None,
        truncate: bool,
        extra_headers: Mapping[str, str] | None,
    ) -> WebResponse:
        current = _checked_url(url)
        headers = {
            **(extra_headers or {}),
            "User-Agent": self.user_agent,
            "Accept-Encoding": "gzip, deflate",
        }
        for _ in range(self.max_redirects + 1):
            request = self.http.build_request("GET", current, headers=headers)
            response = await self.http.send(request, stream=True)
            try:
                location = response.headers.get("location")
                if response.status_code in _REDIRECTS and location:
                    target = normalize_url(location, current)
                    if target is None:
                        raise WebRejectedError(f"{current}: редирект на {location!r}")
                    current = _checked_url(target)
                    if (
                        allowed_hosts is not None
                        and urlsplit(current).hostname not in allowed_hosts
                    ):
                        raise WebRejectedError(f"{url}: редирект за пределы сайта: {current}")
                    continue
                return await _read(response, current, max_bytes, accept, truncate)
            finally:
                await response.aclose()
        raise WebRejectedError(f"{url}: больше {self.max_redirects} редиректов")


def build_web_client(*, user_agent: str, timeout_s: float = DEFAULT_TIMEOUT_S) -> WebClient:
    """Клиент с SSRF-защитой для рабочего окружения; закрывать через `client.http.aclose()`."""
    http = httpx.AsyncClient(transport=GuardedTransport(), timeout=timeout_s, trust_env=False)
    return WebClient(http, user_agent=user_agent, timeout_s=timeout_s)


def _checked_url(url: str) -> str:
    normalized = normalize_url(url)
    if normalized is None:
        raise WebRejectedError(f"{url!r}: не http(s)-URL")
    if urlsplit(normalized).port is not None:
        raise WebRejectedError(f"{url}: разрешены только порты 80 и 443")
    return normalized


async def _read(
    response: httpx.Response,
    url: str,
    max_bytes: int,
    accept: Collection[str] | None,
    truncate: bool,
) -> WebResponse:
    content_type, charset = _content_type(response.headers.get("content-type"))
    ok = response.is_success
    if ok and accept is not None and content_type not in accept:
        raise WebRejectedError(f"{url}: неподходящий Content-Type {content_type!r}")
    body, truncated = await _body(response, url, max_bytes, truncate) if ok else (b"", False)
    return WebResponse(
        url=url,
        status=response.status_code,
        content_type=content_type,
        charset=charset,
        body=body,
        etag=response.headers.get("etag"),
        last_modified=response.headers.get("last-modified"),
        truncated=truncated,
    )


async def _body(
    response: httpx.Response, url: str, max_bytes: int, truncate: bool
) -> tuple[bytes, bool]:
    encoding = response.headers.get("content-encoding", "identity").strip().lower()
    if encoding not in ("identity", "", *_DECODERS):
        raise WebRejectedError(f"{url}: неподдерживаемый Content-Encoding {encoding!r}")
    decoder = zlib.decompressobj(wbits=_DECODERS[encoding]) if encoding in _DECODERS else None
    body = bytearray()
    try:
        async for raw in response.aiter_raw():
            # max_length ограничивает распакованный кусок: лишнее остаётся в unconsumed_tail.
            body += decoder.decompress(raw, max_bytes + 1 - len(body)) if decoder else raw
            if len(body) > max_bytes:
                break
        else:
            if decoder is not None:
                body += decoder.flush()
    except zlib.error as e:
        raise WebFetchError(f"{url}: битое сжатие: {e}") from e
    if len(body) <= max_bytes:
        return bytes(body), False
    if truncate:
        return bytes(body[:max_bytes]), True
    raise WebRejectedError(f"{url}: ответ больше {max_bytes} байт")


def _content_type(header: str | None) -> tuple[str | None, str | None]:
    if not header:
        return None, None
    mime, *params = (part.strip() for part in header.split(";"))
    charset = None
    for param in params:
        name, _, value = param.partition("=")
        if name.strip().lower() == "charset":
            charset = value.strip().strip("\"'") or None
    return mime.lower() or None, charset
