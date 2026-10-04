"""Коннектор `website`: обход сайта по sitemap и ссылкам, страница — документ (ADR-0013).

Сеть — `WebClient` (SSRF-защита, лимиты), robots.txt — `robots`, разбор — `html_extract`,
`sitemap`. Обход в ширину, последовательно, с паузой между запросами: стартовые URL,
страницы из sitemap, затем ссылки (до `max_depth`). Хосты — только хосты `start_urls`.

Полный discover удаляет всё, чего нет в листинге, поэтому листинг строится осторожно:
- документ — 2xx `text/html` без `noindex` с непустым текстом; ключ — URL после редиректов;
- 4xx (кроме 429), `noindex`, не-HTML, слишком большая страница, редирект за пределы
  сайта, запрет robots — страницы нет (документ удалится);
- остальные ошибки (5xx, 429, сеть) временные: URL остаётся в листинге без кэша, `fetch`
  повторит запрос и при неудаче даст ошибку элемента — такие элементы не удаляются;
- недоступны robots.txt, стартовая страница или явный sitemap, вышло `max_duration_s` —
  ошибка синхронизации. `max_pages` обрывает обход; порядок детерминирован.

Разобранные при обходе страницы кэшируются для `fetch`. Курсора нет: каждая синхронизация —
полный обход, неизменённые страницы отсекает `content_hash`.
"""

import asyncio
from collections import OrderedDict, deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.modules.knowledge.domain.entities import SourceKind
from app.modules.knowledge.domain.ingestion import (
    DocumentItem,
    Listing,
    RawItem,
    RawItemRef,
    SourceSpec,
)
from app.modules.knowledge.infrastructure.html_extract import decode_html, extract_page
from app.modules.knowledge.infrastructure.robots import (
    RobotsRules,
    RobotsUnavailableError,
    load_robots,
)
from app.modules.knowledge.infrastructure.sitemap import (
    DEFAULT_MAX_BYTES as SITEMAP_MAX_BYTES,
)
from app.modules.knowledge.infrastructure.sitemap import SitemapError, parse_sitemap
from app.modules.knowledge.infrastructure.web_client import (
    WebClient,
    WebFetchError,
    WebRejectedError,
    WebResponse,
)
from app.modules.knowledge.infrastructure.web_urls import normalize_url
from app.modules.shared.kernel import TenantId

MAX_PAGES_CAP = 5000
MAX_ROBOTS_DELAY_S = 10.0
MAX_PAGE_BYTES = 5 * 1024 * 1024
MAX_SITEMAP_FILES = 50
MAX_SITEMAP_NESTING = 2
DEFAULT_CACHE_SIZE = 4

_HTML = frozenset({"text/html", "application/xhtml+xml"})


class WebsiteSourceError(Exception):
    """Ошибка источника целиком (конфиг, robots, стартовая страница) или его страницы."""


class _PageGoneError(Exception):
    """Страницы нет как документа: 4xx, noindex, не-HTML, пустой текст."""


class WebsiteSourceConfig(BaseModel):
    """`Source.config` источника `website` (architecture.md §8)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    start_urls: list[str] = Field(min_length=1, max_length=20)
    sitemaps: list[str] = Field(default_factory=list, max_length=20)
    follow_links: bool = True
    max_pages: int = Field(default=500, ge=1)
    max_depth: int = Field(default=5, ge=0, le=50)
    include: list[str] = Field(default_factory=list)  # префиксы пути
    exclude: list[str] = Field(default_factory=list)
    crawl_delay_s: float = Field(default=0.5, ge=0, le=60)
    max_duration_s: float = Field(default=1800, gt=0, le=6 * 3600)


@dataclass(slots=True)
class _Site:
    """Состояние одного обхода: правила сайта и пауза между запросами."""

    config: WebsiteSourceConfig
    start_urls: list[str]
    hosts: frozenset[str]
    robots: dict[str, RobotsRules] = field(default_factory=dict)
    delay_s: float = 0.0
    requests: int = 0

    def in_scope(self, url: str) -> bool:
        """Хост источника и include/exclude; стартовые URL фильтры не отсекают."""
        if url in self.start_urls:
            return True
        parts = urlsplit(url)
        path = parts.path or "/"
        return (
            parts.hostname in self.hosts
            and (not self.config.include or any(path.startswith(p) for p in self.config.include))
            and not any(path.startswith(p) for p in self.config.exclude)
        )

    def allowed(self, url: str) -> bool:
        return self.in_scope(url) and self.robots[_origin(url)].allowed(url)


class WebsiteConnector:
    kind = SourceKind.WEBSITE

    def __init__(
        self,
        client: WebClient,
        *,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        max_pages_cap: int = MAX_PAGES_CAP,
        cache_size: int = DEFAULT_CACHE_SIZE,
    ) -> None:
        self.client = client
        self.sleep = sleep
        self.max_pages_cap = max_pages_cap
        self.cache_size = cache_size
        # Страницы последнего обхода источника: fetch берёт их, не скачивая заново.
        self._cache: OrderedDict[tuple[TenantId, UUID], dict[str, DocumentItem]] = OrderedDict()

    async def discover(self, source: SourceSpec) -> Listing:
        site = await self._open(source)
        try:
            async with asyncio.timeout(site.config.max_duration_s):
                refs, pages = await self._crawl(site)
        except TimeoutError as e:
            raise WebsiteSourceError(f"обход не уложился в {site.config.max_duration_s} с") from e
        key = (source.tenant_id, source.source_id)
        self._cache[key] = pages
        self._cache.move_to_end(key)
        while len(self._cache) > self.cache_size:
            self._cache.popitem(last=False)
        return Listing(refs)

    async def changed_since(self, source: SourceSpec, cursor: str) -> Listing:
        return await self.discover(source)

    async def fetch(self, source: SourceSpec, ref: RawItemRef) -> RawItem:
        cached = self._cache.get((source.tenant_id, source.source_id), {})
        if ref.external_id in cached:
            return cached[ref.external_id]
        site = await self._open(source)
        if not site.allowed(ref.external_id):
            raise WebsiteSourceError(f"{ref.external_id}: вне источника или запрещено robots.txt")
        try:
            return await self._page(site, ref.external_id)
        except (WebFetchError, _PageGoneError) as e:
            raise WebsiteSourceError(str(e)) from e

    async def _open(self, source: SourceSpec) -> _Site:
        config = _parse_config(source.config)
        starts = [normalize_url(u) for u in config.start_urls]
        if None in starts:
            raise WebsiteSourceError(f"start_urls: не http(s)-URL в {config.start_urls}")
        start_urls = [u for u in dict.fromkeys(starts) if u is not None]
        site = _Site(
            config,
            start_urls,
            frozenset(urlsplit(u).hostname or "" for u in start_urls),
            delay_s=config.crawl_delay_s,
        )
        for origin in dict.fromkeys(_origin(u) for u in start_urls):
            try:
                rules = await load_robots(self.client, origin, self.client.user_agent)
            except RobotsUnavailableError as e:
                raise WebsiteSourceError(f"robots.txt недоступен: {e}") from e
            site.robots[origin] = rules
            if rules.crawl_delay is not None:
                site.delay_s = max(site.delay_s, min(rules.crawl_delay, MAX_ROBOTS_DELAY_S))
        return site

    async def _crawl(self, site: _Site) -> tuple[list[RawItemRef], dict[str, DocumentItem]]:
        limit = min(site.config.max_pages, self.max_pages_cap)
        queue: deque[tuple[str, int]] = deque((u, 0) for u in site.start_urls)
        queue.extend((u, 0) for u in await self._sitemap_pages(site))
        queued = {url for url, _ in queue}
        refs: dict[str, RawItemRef] = {}
        pages: dict[str, DocumentItem] = {}
        visited = 0
        while queue and visited < limit:
            url, depth = queue.popleft()
            if not site.allowed(url):
                if url in site.start_urls:
                    # Иначе листинг опустеет и полный discover удалит документы источника.
                    raise WebsiteSourceError(f"стартовая страница запрещена robots.txt: {url}")
                continue
            visited += 1
            try:
                page, links = await self._visit(site, url)
            except _PageGoneError:
                if url in site.start_urls:
                    raise WebsiteSourceError(f"стартовая страница недоступна: {url}") from None
                continue
            except WebFetchError as e:
                if url in site.start_urls:
                    raise WebsiteSourceError(f"стартовая страница недоступна: {e}") from e
                refs.setdefault(url, RawItemRef(url))  # временная ошибка: fetch повторит
                continue
            if page is not None and page.external_id not in pages:
                pages[page.external_id] = page
                refs[page.external_id] = RawItemRef(page.external_id)
            if site.config.follow_links and depth < site.config.max_depth:
                for link in links:
                    if link not in queued and site.in_scope(link):
                        queued.add(link)
                        queue.append((link, depth + 1))
        return list(refs.values()), pages

    async def _visit(self, site: _Site, url: str) -> tuple[DocumentItem | None, list[str]]:
        """Страница и её ссылки; `None` вместо страницы — noindex или пустой текст
        (ссылки при этом нужны)."""
        try:
            response = await self._get(site, url, max_bytes=MAX_PAGE_BYTES, accept=_HTML)
        except WebRejectedError as e:
            raise _PageGoneError(str(e)) from e
        if response.status == 429 or response.status >= 500:
            raise WebFetchError(f"{url}: HTTP {response.status}")
        if not 200 <= response.status < 300:
            raise _PageGoneError(f"{url}: HTTP {response.status}")
        if not site.allowed(response.url):
            raise _PageGoneError(f"{url}: редирект на запрещённый адрес {response.url}")
        extracted = extract_page(decode_html(response.body, response.charset), response.url)
        if extracted.noindex or not extracted.text.strip():
            return None, extracted.links
        page = DocumentItem(
            external_id=response.url,
            title=extracted.title or response.url,
            text=extracted.text,
            url=response.url,
        )
        return page, extracted.links

    async def _page(self, site: _Site, url: str) -> DocumentItem:
        page, _ = await self._visit(site, url)
        if page is None:
            raise _PageGoneError(f"{url}: noindex или пустая страница")
        return page

    async def _sitemap_pages(self, site: _Site) -> list[str]:
        explicit = [u for u in (normalize_url(s) for s in site.config.sitemaps) if u]
        from_robots = [s for rules in site.robots.values() for s in rules.sitemaps]
        fallback = [] if explicit or from_robots else [f"{o}/sitemap.xml" for o in site.robots]
        pages: list[str] = []
        pending = [(u, 0, u in explicit) for u in dict.fromkeys(explicit + from_robots + fallback)]
        seen = {u for u, _, _ in pending}
        files = 0
        while pending and files < MAX_SITEMAP_FILES:
            url, nesting, required = pending.pop(0)
            files += 1
            try:
                response = await self._get(site, url, max_bytes=SITEMAP_MAX_BYTES)
                if not 200 <= response.status < 300:
                    raise WebFetchError(f"{url}: HTTP {response.status}")
                sitemap = parse_sitemap(response.body)
            except (WebFetchError, SitemapError) as e:
                if required:
                    raise WebsiteSourceError(f"sitemap недоступен: {e}") from e
                continue
            pages += [e.url for e in sitemap.pages if site.in_scope(e.url)]
            if nesting < MAX_SITEMAP_NESTING:
                for entry in sitemap.sitemaps:
                    if entry.url not in seen and urlsplit(entry.url).hostname in site.hosts:
                        seen.add(entry.url)
                        pending.append((entry.url, nesting + 1, required))
        return list(dict.fromkeys(pages))

    async def _get(self, site: _Site, url: str, **kwargs: Any) -> WebResponse:
        if site.requests and site.delay_s:
            await self.sleep(site.delay_s)
        site.requests += 1
        return await self.client.get(url, allowed_hosts=site.hosts, **kwargs)


def _origin(url: str) -> str:
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}"


def _parse_config(config: dict[str, Any]) -> WebsiteSourceConfig:
    try:
        return WebsiteSourceConfig.model_validate(config)
    except ValidationError as e:
        raise WebsiteSourceError(f"неверный конфиг источника website: {e}") from e
