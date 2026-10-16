"""Коннектор `website`: обход по sitemap и ссылкам, robots, что попадает в листинг, кэш."""

import asyncio
import threading
from typing import Any
from uuid import uuid4

import httpx
import pytest

from app.modules.knowledge.infrastructure.html_extract import extract_page
from app.modules.knowledge.infrastructure.sitemap import parse_sitemap
from app.modules.knowledge.public import (
    DocumentItem,
    RawItemRef,
    SourceConnector,
    SourceSpec,
    WebClient,
    WebsiteConnector,
    WebsiteSourceError,
)
from app.modules.shared.public import TenantId

SITE = "https://example.ru"
SM_NS = 'xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"'


def page(*links: str, text: str = "Текст страницы.", head: str = "") -> tuple[int, str, str]:
    anchors = "".join(f'<a href="{href}">ссылка</a>' for href in links)
    html = f"<html><head>{head}</head><body><main><p>{text}</p>{anchors}</main></body></html>"
    return 200, "text/html; charset=utf-8", html


def urlset(*paths: str) -> tuple[int, str, str]:
    urls = "".join(f"<url><loc>{SITE}{p}</loc></url>" for p in paths)
    return 200, "application/xml", f"<urlset {SM_NS}>{urls}</urlset>"


class Site:
    """Сайт на `httpx.MockTransport`: путь → (статус, Content-Type, тело) или (3xx, Location).
    Ключ — полный URL, если ответ зависит от схемы; иначе путь (любая схема)."""

    def __init__(self, routes: dict[str, Any]) -> None:
        self.routes = {"/robots.txt": (404, "text/plain", ""), **routes}
        self.requests: list[str] = []
        self.urls: list[str] = []
        self.sleeps: list[float] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.raw_path.decode()
        url = f"{request.url.scheme}://{request.url.netloc.decode()}{path}"
        self.requests.append(path)
        self.urls.append(url)
        route = self.routes.get(url) or self.routes.get(path, (404, "text/html", "нет"))
        if isinstance(route, Exception):
            raise route
        if len(route) == 2:
            return httpx.Response(route[0], headers={"Location": route[1]})
        status, content_type, body = route
        headers = {"Content-Type": content_type}
        return httpx.Response(status, headers=headers, stream=httpx.ByteStream(body.encode()))

    def connector(self) -> WebsiteConnector:
        async def sleep(seconds: float) -> None:
            self.sleeps.append(seconds)

        http = httpx.AsyncClient(transport=httpx.MockTransport(self.handler))
        return WebsiteConnector(WebClient(http, user_agent="TesseraBot/0.1"), sleep=sleep)


def spec(**config: Any) -> SourceSpec:
    return SourceSpec(TenantId(uuid4()), uuid4(), {"start_urls": [f"{SITE}/"], **config})


async def listed(site: Site, **config: Any) -> list[str]:
    listing = await site.connector().discover(spec(**config))
    assert listing.cursor is None
    return sorted(ref.external_id.removeprefix(SITE) for ref in listing.refs)


async def test_crawls_links_within_hosts_and_depth() -> None:
    site = Site(
        {
            "/": page("/a", "https://other.ru/x", "mailto:a@b.ru"),
            "/a": page("/b"),
            "/b": page("/c"),
            "/c": page(),
        }
    )
    assert await listed(site, max_depth=2) == ["/", "/a", "/b"]
    assert "/c" not in site.requests


async def test_sitemap_index_from_robots_include_exclude_and_max_pages() -> None:
    robots = f"User-agent: *\nDisallow: /secret\nSitemap: {SITE}/index.xml\n"
    index = f"<sitemapindex {SM_NS}><sitemap><loc>{SITE}/pages.xml</loc></sitemap></sitemapindex>"
    site = Site(
        {
            "/robots.txt": (200, "text/plain", robots),
            "/index.xml": (200, "application/xml", index),
            "/pages.xml": urlset("/docs/1", "/docs/2", "/docs/old", "/secret/x", "/blog/1"),
            "/": page(),
            **{p: page() for p in ("/docs/1", "/docs/2", "/docs/old", "/secret/x", "/blog/1")},
        }
    )
    config = {"follow_links": False, "include": ["/docs/"], "exclude": ["/docs/old"]}
    # Стартовую страницу include не отсекает.
    assert await listed(site, **config) == ["/", "/docs/1", "/docs/2"]
    assert "/secret/x" not in site.requests
    assert await listed(site, **config, max_pages=2) == ["/", "/docs/1"]


async def test_default_sitemap_is_optional() -> None:
    site = Site({"/": page("/a"), "/a": page()})
    assert await listed(site) == ["/", "/a"]
    assert "/sitemap.xml" in site.requests


async def test_what_is_not_a_document() -> None:
    site = Site(
        {
            "/": page("/gone", "/noindex", "/pdf", "/empty", "/moved", "/away", "/forbidden"),
            "/gone": (410, "text/html", ""),
            "/forbidden": (403, "text/html", ""),
            "/noindex": page("/deep", head='<meta name="robots" content="noindex">'),
            "/deep": page(),
            "/pdf": (200, "application/pdf", "%PDF"),
            "/empty": page(text=""),
            "/moved": (301, "/"),
            "/away": (302, "https://other.ru/"),
        }
    )
    # Ссылки со страницы noindex обходятся; редирект на известную страницу — не дубль.
    assert await listed(site) == ["/", "/deep"]


async def test_transient_errors_stay_in_listing_and_fail_on_fetch() -> None:
    site = Site(
        {
            "/": page("/busy", "/down", "/slow"),
            "/busy": (429, "text/html", ""),
            "/down": (503, "text/html", ""),
            "/slow": httpx.ConnectTimeout("timeout"),
        }
    )
    connector = site.connector()
    source = spec()
    listing = await connector.discover(source)
    urls = sorted(r.external_id.removeprefix(SITE) for r in listing.refs)
    assert urls == ["/", "/busy", "/down", "/slow"]
    # run_sync не удаляет элементы, на которых fetch упал (test_ingestion).
    with pytest.raises(WebsiteSourceError, match="503"):
        await connector.fetch(source, RawItemRef(f"{SITE}/down"))


@pytest.mark.parametrize(
    "routes",
    [
        {"/robots.txt": (500, "text/plain", "")},
        {"/robots.txt": (200, "text/plain", "User-agent: *\nDisallow: /")},
        {"/": (503, "text/html", "")},
        {"/": (404, "text/html", "")},
        {"/": page(), "/sitemap-main.xml": (500, "application/xml", "")},
    ],
    ids=["robots-5xx", "start-disallowed", "start-5xx", "start-404", "explicit-sitemap"],
)
async def test_source_level_errors(routes: dict[str, Any]) -> None:
    site = Site(routes)
    with pytest.raises(WebsiteSourceError):
        await site.connector().discover(spec(sitemaps=[f"{SITE}/sitemap-main.xml"]))


async def test_invalid_config() -> None:
    site = Site({})
    connector = site.connector()
    with pytest.raises(WebsiteSourceError, match="конфиг"):
        await connector.discover(SourceSpec(TenantId(uuid4()), uuid4(), {"start_urls": []}))
    with pytest.raises(WebsiteSourceError, match="start_urls"):
        await connector.discover(spec(start_urls=["ftp://example.ru/"]))


async def test_redirect_to_other_scheme_reads_its_robots() -> None:
    """Адрес введён без https, сайт перенаправляет на https: robots читается и для него."""
    site = Site(
        {
            "http://example.ru/": (301, f"{SITE}/"),
            f"{SITE}/": page("/a"),
            "/a": page(text="Страница А"),
        }
    )
    assert await listed(site, start_urls=["http://example.ru/"]) == ["/", "/a"]
    assert "http://example.ru/robots.txt" in site.urls
    assert f"{SITE}/robots.txt" in site.urls


async def test_link_with_other_scheme_follows_that_origin_robots() -> None:
    site = Site(
        {
            "/": page("http://example.ru/b", "http://example.ru/c"),
            "http://example.ru/robots.txt": (200, "text/plain", "User-agent: *\nDisallow: /b"),
            "/b": page(),
            "/c": page(),
        }
    )
    listing = await site.connector().discover(spec())
    assert sorted(ref.external_id for ref in listing.refs) == ["http://example.ru/c", f"{SITE}/"]
    assert "http://example.ru/b" not in site.urls


async def test_unavailable_robots_of_other_origin_forbids_only_its_pages() -> None:
    site = Site(
        {
            "/": page("http://example.ru/c"),
            "http://example.ru/robots.txt": (503, "text/plain", ""),
            "/c": page(),
        }
    )
    assert await listed(site) == ["/"]
    assert "http://example.ru/c" not in site.urls


async def test_duration_limit() -> None:
    async def slow(request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(0.2)
        return httpx.Response(404)

    http = httpx.AsyncClient(transport=httpx.MockTransport(slow))
    connector = WebsiteConnector(WebClient(http, user_agent="TesseraBot/0.1"))
    with pytest.raises(WebsiteSourceError, match="не уложился"):
        await connector.discover(spec(max_duration_s=0.1))


async def test_fetch_uses_crawl_cache_then_network() -> None:
    site = Site({"/": page("/a", text="Главная"), "/a": page(text="Страница А")})
    connector = site.connector()
    source = spec(follow_links=True)
    await connector.discover(source)
    requests = len(site.requests)

    item = await connector.fetch(source, RawItemRef(f"{SITE}/a"))
    assert isinstance(item, DocumentItem)
    assert (item.url, item.text) == (f"{SITE}/a", "Страница А")
    assert len(site.requests) == requests

    # Без кэша (другой источник или после release) — скачивается заново, с проверкой robots.
    other = spec()
    fresh = await connector.fetch(other, RawItemRef(f"{SITE}/a"))
    assert fresh == item
    assert site.requests[requests:] == ["/robots.txt", "/a"]
    with pytest.raises(WebsiteSourceError, match="вне источника"):
        await connector.fetch(other, RawItemRef("https://other.ru/a"))


async def test_crawl_delay_from_config_and_robots() -> None:
    site = Site({"/": page("/a"), "/a": page()})
    await site.connector().discover(spec(crawl_delay_s=0.2))
    assert site.sleeps and set(site.sleeps) == {0.2}

    site = Site({"/robots.txt": (200, "text/plain", "User-agent: *\nCrawl-delay: 60"), "/": page()})
    await site.connector().discover(spec())
    assert set(site.sleeps) == {10.0}  # Crawl-delay из robots ограничен сверху


async def test_fetch_miss_keeps_crawl_robots_and_delay() -> None:
    """K-11: страница с временной ошибкой обхода запрашивается в fetch без повторного
    robots.txt и с паузой между запросами."""
    robots = "User-agent: *\nCrawl-delay: 2"
    site = Site(
        {
            "/robots.txt": (200, "text/plain", robots),
            "/": page("/down", "/busy"),
            "/down": (503, "text/html", ""),
            "/busy": (503, "text/html", ""),
        }
    )
    connector = site.connector()
    source = spec(crawl_delay_s=0.1)
    await connector.discover(source)
    requests, sleeps = len(site.requests), len(site.sleeps)
    site.routes["/down"] = site.routes["/busy"] = page(text="Ожила")

    for path in ("/down", "/busy"):
        item = await connector.fetch(source, RawItemRef(f"{SITE}{path}"))
        assert isinstance(item, DocumentItem) and item.text == "Ожила"
    assert site.requests[requests:] == ["/down", "/busy"]
    assert site.sleeps[sleeps:] == [2.0, 2.0]


async def test_release_forgets_crawl_of_source() -> None:
    """K-5, K-13: обход источника живёт до release, без вытеснения другими источниками."""
    site = Site({"/": page("/a"), "/a": page(text="Страница А")})
    connector = site.connector()
    sources = [spec() for _ in range(6)]
    for source in sources:
        await connector.discover(source)
    requests = len(site.requests)

    for source in sources:
        await connector.fetch(source, RawItemRef(f"{SITE}/a"))
    assert len(site.requests) == requests

    connector.release(sources[0])
    await connector.fetch(sources[0], RawItemRef(f"{SITE}/a"))
    await connector.fetch(sources[0], RawItemRef(f"{SITE}/a"))
    # Без обхода robots.txt читается один раз до release.
    assert site.requests[requests:] == ["/robots.txt", "/a", "/a"]


async def test_parsing_runs_off_event_loop(monkeypatch: pytest.MonkeyPatch) -> None:
    """K-7: разбор HTML (до 5 МБ) и sitemap (до 50 МБ) не блокирует event loop."""
    threads: dict[str, int] = {}

    def spy(name: str, func: Any) -> Any:
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            threads[name] = threading.get_ident()
            return func(*args, **kwargs)

        return wrapper

    monkeypatch.setattr(
        "app.modules.knowledge.infrastructure.website_connector.extract_page",
        spy("html", extract_page),
    )
    monkeypatch.setattr(
        "app.modules.knowledge.infrastructure.website_connector.parse_sitemap",
        spy("sitemap", parse_sitemap),
    )
    site = Site({"/sitemap.xml": urlset("/a"), "/": page(), "/a": page()})

    assert await listed(site) == ["/", "/a"]
    assert set(threads) == {"html", "sitemap"}
    assert threading.get_ident() not in threads.values()


def test_satisfies_protocol() -> None:
    connector: SourceConnector = Site({}).connector()
    assert connector.kind == "website"
