"""HTTP-клиент краулера `website`: редиректы, лимиты, Content-Type, сжатие (ADR-0013)."""

import asyncio
import gzip
from collections.abc import Callable

import httpx
import pytest

from app.modules.knowledge.infrastructure.web_client import WebClient, WebFetchError

HTML = {"text/html"}


def streamed(response: httpx.Response) -> httpx.Response:
    """Ответ, как из сети: тело ещё не прочитано (клиент читает его через `aiter_raw`)."""
    try:
        content = response.content
    except httpx.ResponseNotRead:
        return response
    return httpx.Response(
        response.status_code, headers=response.headers, stream=httpx.ByteStream(content)
    )


def _client(
    handler: Callable[[httpx.Request], httpx.Response],
) -> tuple[WebClient, list[httpx.Request]]:
    seen: list[httpx.Request] = []

    def record(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return streamed(handler(request))

    http = httpx.AsyncClient(transport=httpx.MockTransport(record))
    return WebClient(http, user_agent="TesseraBot/0.1"), seen


def _compressed(encoding: str, body: bytes) -> httpx.Response:
    return httpx.Response(
        200, headers={"Content-Encoding": encoding}, stream=httpx.ByteStream(body)
    )


def _routes(routes: dict[str, httpx.Response]) -> Callable[[httpx.Request], httpx.Response]:
    return lambda request: routes.get(str(request.url), httpx.Response(404))


async def test_get_returns_body_and_metadata() -> None:
    response = httpx.Response(
        200,
        headers={"Content-Type": 'text/html; charset="windows-1251"', "ETag": '"v1"'},
        content=b"<p>hi</p>",
    )
    client, seen = _client(_routes({"https://example.ru/": response}))
    result = await client.get("HTTPS://Example.ru", max_bytes=100, accept=HTML)
    assert (result.url, result.status, result.body) == ("https://example.ru/", 200, b"<p>hi</p>")
    assert (result.content_type, result.charset, result.etag) == (
        "text/html",
        "windows-1251",
        '"v1"',
    )
    assert seen[0].headers["user-agent"] == "TesseraBot/0.1"


async def test_follows_redirects_within_allowed_hosts() -> None:
    client, _ = _client(
        _routes(
            {
                "http://example.ru/a": httpx.Response(301, headers={"Location": "/b"}),
                "http://example.ru/b": httpx.Response(
                    302, headers={"Location": "https://www.example.ru/c#x"}
                ),
                "https://www.example.ru/c": httpx.Response(200, text="ok"),
            }
        )
    )
    result = await client.get(
        "http://example.ru/a", max_bytes=100, allowed_hosts={"example.ru", "www.example.ru"}
    )
    assert (result.url, result.body) == ("https://www.example.ru/c", b"ok")


@pytest.mark.parametrize(
    ("location", "message"),
    [
        ("https://evil.ru/", "за пределы сайта"),
        ("ftp://example.ru/file", "редирект на"),
        ("http://example.ru:8080/", "порты"),
    ],
)
async def test_rejects_bad_redirects(location: str, message: str) -> None:
    client, seen = _client(lambda _: httpx.Response(302, headers={"Location": location}))
    with pytest.raises(WebFetchError, match=message):
        await client.get("http://example.ru/", max_bytes=100, allowed_hosts={"example.ru"})
    assert len(seen) == 1


async def test_redirect_limit() -> None:
    client, seen = _client(lambda r: httpx.Response(302, headers={"Location": f"{r.url}x"}))
    with pytest.raises(WebFetchError, match="редиректов"):
        await client.get("http://example.ru/", max_bytes=100)
    assert len(seen) == 6


@pytest.mark.parametrize(
    "url", ["file:///etc/passwd", "http://example.ru:6379/", "gopher://example.ru/"]
)
async def test_rejects_urls_before_request(url: str) -> None:
    client, seen = _client(lambda _: httpx.Response(200))
    with pytest.raises(WebFetchError):
        await client.get(url, max_bytes=100)
    assert seen == []


async def test_rejects_unexpected_content_type() -> None:
    client, _ = _client(lambda _: httpx.Response(200, headers={"Content-Type": "image/png"}))
    with pytest.raises(WebFetchError, match="Content-Type"):
        await client.get("http://example.ru/", max_bytes=100, accept=HTML)


async def test_non_success_status_returned_without_body() -> None:
    client, _ = _client(lambda _: httpx.Response(503, text="down"))
    result = await client.get("http://example.ru/", max_bytes=100, accept=HTML)
    assert (result.status, result.body) == (503, b"")


async def test_body_limit_and_truncate() -> None:
    client, _ = _client(lambda _: httpx.Response(200, content=b"x" * 1000))
    with pytest.raises(WebFetchError, match="больше 100 байт"):
        await client.get("http://example.ru/", max_bytes=100)
    result = await client.get("http://example.ru/", max_bytes=100, truncate=True)
    assert (len(result.body), result.truncated) == (100, True)


async def test_gzip_body_is_decoded() -> None:
    client, _ = _client(lambda _: _compressed("gzip", gzip.compress(b"hello" * 10)))
    assert (await client.get("http://example.ru/", max_bytes=100)).body == b"hello" * 10


async def test_gzip_bomb_stops_at_limit() -> None:
    bomb = gzip.compress(b"\0" * 50_000_000)
    assert len(bomb) < 100_000
    client, _ = _client(lambda _: _compressed("gzip", bomb))
    with pytest.raises(WebFetchError, match="больше"):
        await client.get("http://example.ru/", max_bytes=1_000_000)


async def test_broken_or_unknown_encoding() -> None:
    client, _ = _client(lambda r: _compressed(r.url.path.strip("/"), b"not compressed"))
    with pytest.raises(WebFetchError, match="битое сжатие"):
        await client.get("http://example.ru/gzip", max_bytes=100)
    with pytest.raises(WebFetchError, match="Content-Encoding"):
        await client.get("http://example.ru/zstd", max_bytes=100)


async def test_network_errors_and_timeout() -> None:
    def fail(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    client, _ = _client(fail)
    with pytest.raises(WebFetchError, match="ConnectError"):
        await client.get("http://example.ru/", max_bytes=100)

    async def slow(request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(1)
        return httpx.Response(200)

    http = httpx.AsyncClient(transport=httpx.MockTransport(slow))
    client = WebClient(http, user_agent="TesseraBot/0.1", timeout_s=0.05)
    with pytest.raises(WebFetchError, match="нет ответа"):
        await client.get("http://example.ru/", max_bytes=100)


async def test_extra_headers_sent_on_every_redirect_hop() -> None:
    routes = {
        "https://example.ru/a": httpx.Response(302, headers={"Location": "/b"}),
        "https://example.ru/b": httpx.Response(200, content=b"ok"),
    }
    client, seen = _client(_routes(routes))
    await client.get(
        "https://example.ru/a", max_bytes=10, headers={"X-Api-Key": "k", "Accept": "*/*"}
    )
    assert [r.headers["X-Api-Key"] for r in seen] == ["k", "k"]
    assert seen[0].headers["User-Agent"] == "TesseraBot/0.1"
