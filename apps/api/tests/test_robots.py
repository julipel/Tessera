"""robots.txt краулера `website` (RFC 9309, ADR-0013)."""

import httpx
import pytest

from app.modules.knowledge.infrastructure.robots import (
    RobotsUnavailableError,
    load_robots,
    parse_robots,
)
from app.modules.knowledge.infrastructure.web_client import WebClient

UA = "TesseraBot/0.1"
BASE = "https://example.ru/robots.txt"

ROBOTS = """
# комментарий
User-agent: *
Disallow: /private/
Disallow: /*?sort=
Disallow: /*.pdf$
Allow: /private/public/
Crawl-delay: 2

User-agent: OtherBot
Disallow: /

Sitemap: https://example.ru/sitemap.xml
Sitemap: /sitemap-2.xml
"""


@pytest.mark.parametrize(
    ("url", "allowed"),
    [
        ("https://example.ru/", True),
        ("https://example.ru/private/x", False),
        ("https://example.ru/private/public/x", True),  # длиннее правило Allow
        ("https://example.ru/catalog?sort=price", False),
        ("https://example.ru/catalog?page=2", True),
        ("https://example.ru/doc.pdf", False),
        ("https://example.ru/doc.pdf?v=1", True),  # `$` — конец URL
        ("https://example.ru/robots.txt", True),
    ],
)
def test_wildcard_group_rules(url: str, allowed: bool) -> None:
    rules = parse_robots(ROBOTS, UA, BASE)
    assert rules.allowed(url) is allowed


def test_crawl_delay_and_sitemaps() -> None:
    rules = parse_robots(ROBOTS, UA, BASE)
    assert rules.crawl_delay == 2.0
    assert rules.sitemaps == ("https://example.ru/sitemap.xml", "https://example.ru/sitemap-2.xml")


def test_own_group_replaces_star_and_groups_merge() -> None:
    text = """
User-agent: *
Disallow: /

User-agent: googlebot
User-agent: TESSERABOT
Disallow: /a

User-agent: tesserabot
Disallow: /b
"""
    rules = parse_robots(text, UA, BASE)
    assert [rules.allowed(f"https://example.ru{p}") for p in ("/", "/a", "/b")] == [
        True,
        False,
        False,
    ]


def test_equal_length_allow_wins_and_empty_disallow() -> None:
    rules = parse_robots("User-agent: *\nDisallow: /page\nAllow: /page\n", UA, BASE)
    assert rules.allowed("https://example.ru/page")
    assert parse_robots("User-agent: *\nDisallow:\n", UA, BASE).allowed("https://example.ru/x")


def test_percent_encoding_is_canonical() -> None:
    rules = parse_robots("User-agent: *\nDisallow: /каталог/\n", UA, BASE)
    assert not rules.allowed("https://example.ru/%D0%BA%D0%B0%D1%82%D0%B0%D0%BB%D0%BE%D0%B3/1")


def _response(status: int, text: str = "") -> httpx.Response:
    """Потоковый ответ, как из сети: клиент читает тело через `aiter_raw`."""
    return httpx.Response(status, stream=httpx.ByteStream(text.encode()))


def _client(response: httpx.Response | Exception) -> WebClient:
    def handler(request: httpx.Request) -> httpx.Response:
        if isinstance(response, Exception):
            raise response
        return response

    return WebClient(httpx.AsyncClient(transport=httpx.MockTransport(handler)), user_agent=UA)


async def test_load_robots_statuses() -> None:
    rules = await load_robots(
        _client(_response(200, "User-agent: *\nDisallow: /x")), "https://example.ru", UA
    )
    assert not rules.allowed("https://example.ru/x")

    rules = await load_robots(_client(_response(404)), "https://example.ru", UA)
    assert rules.allowed("https://example.ru/x")

    with pytest.raises(RobotsUnavailableError, match="503"):
        await load_robots(_client(_response(503)), "https://example.ru", UA)
    with pytest.raises(RobotsUnavailableError):
        await load_robots(_client(httpx.ConnectTimeout("t")), "https://example.ru", UA)


async def test_load_robots_truncates_large_file() -> None:
    text = "User-agent: *\nDisallow: /x\n" + "#" * 2_000_000
    rules = await load_robots(_client(_response(200, text)), "https://example.ru", UA)
    assert not rules.allowed("https://example.ru/x")
