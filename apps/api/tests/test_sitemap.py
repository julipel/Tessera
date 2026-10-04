"""Нормализация URL и разбор sitemap для коннектора `website`."""

import gzip
from datetime import UTC, datetime, timedelta, timezone

import pytest

from app.modules.knowledge.infrastructure.sitemap import (
    SitemapEntry,
    SitemapError,
    parse_lastmod,
    parse_sitemap,
)
from app.modules.knowledge.infrastructure.web_urls import normalize_url

NS = 'xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"'


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("HTTPS://Example.RU:443/Path?b=2&a=1#frag", "https://example.ru/Path?b=2&a=1"),
        ("http://example.ru", "http://example.ru/"),
        ("http://example.ru:8080/x", "http://example.ru:8080/x"),
        ("https://пример.рф/о-нас", "https://xn--e1afmkfd.xn--p1ai/о-нас"),
        ("http://[::1]:80/", "http://[::1]/"),
        ("  https://example.ru/x  ", "https://example.ru/x"),
        ("ftp://example.ru/file", None),
        ("mailto:a@example.ru", None),
        ("https://user:pass@example.ru/", None),
        ("https://example.ru:99999/", None),
        ("https:///path", None),
        ("/relative", None),
    ],
)
def test_normalize_url(url: str, expected: str | None) -> None:
    assert normalize_url(url) == expected


def test_normalize_url_resolves_relative_to_base() -> None:
    assert normalize_url("../b?x=1", "https://example.ru/a/c/d") == "https://example.ru/a/b?x=1"
    assert normalize_url("//cdn.example.ru/x", "https://example.ru/") == "https://cdn.example.ru/x"


def test_urlset() -> None:
    data = f"""<?xml version="1.0" encoding="UTF-8"?>
    <urlset {NS}>
      <url><loc> https://Example.ru/a </loc><lastmod>2026-09-01</lastmod></url>
      <url><loc>https://example.ru/b#x</loc><changefreq>daily</changefreq></url>
      <url><loc>https://example.ru/a</loc><lastmod>2027-01-01</lastmod></url>
      <url><loc>not a url</loc></url>
      <url><lastmod>2026-09-01</lastmod></url>
    </urlset>""".encode()
    sitemap = parse_sitemap(data)
    assert sitemap.sitemaps == []
    assert sitemap.pages == [
        SitemapEntry("https://example.ru/a", datetime(2026, 9, 1, tzinfo=UTC)),
        SitemapEntry("https://example.ru/b"),
    ]


def test_sitemap_index_without_namespace() -> None:
    data = b"""<sitemapindex>
      <sitemap><loc>https://example.ru/sitemap-1.xml.gz</loc>
        <lastmod>2026-09-01T10:00:00+03:00</lastmod></sitemap>
    </sitemapindex>"""
    sitemap = parse_sitemap(data)
    assert sitemap.pages == []
    assert sitemap.sitemaps == [
        SitemapEntry("https://example.ru/sitemap-1.xml.gz", datetime(2026, 9, 1, 7, tzinfo=UTC))
    ]


def test_gzip_and_bom() -> None:
    data = gzip.compress(f"﻿<urlset {NS}><url><loc>https://e.ru/</loc></url></urlset>".encode())
    assert parse_sitemap(data).pages == [SitemapEntry("https://e.ru/")]


def test_text_sitemap() -> None:
    data = b"https://example.ru/a\r\n\r\nhttps://example.ru/b\n"
    assert [e.url for e in parse_sitemap(data).pages] == [
        "https://example.ru/a",
        "https://example.ru/b",
    ]


def test_empty_sitemap_is_empty() -> None:
    assert parse_sitemap(f"<urlset {NS}></urlset>".encode()).pages == []
    assert parse_sitemap(b"").pages == []


@pytest.mark.parametrize(
    "data",
    [
        b"<urlset><url><loc>https://e.ru/</loc></urlset>",
        b"<!doctype html><html><body>Not found</body></html>",
        b"<html><body>Not found</body></html>",
        b'<?xml version="1.0"?><!DOCTYPE lolz [<!ENTITY lol "lol">]><urlset>&lol;</urlset>',
        b"\x1f\x8bnot really gzip",
        b"just some text, no urls",
        "кириллица".encode("cp1251"),
    ],
)
def test_broken_sitemap_is_an_error(data: bytes) -> None:
    with pytest.raises(SitemapError):
        parse_sitemap(data)


def test_limits() -> None:
    entries = "".join(f"<url><loc>https://e.ru/{i}</loc></url>" for i in range(3))
    data = f"<urlset {NS}>{entries}</urlset>".encode()
    assert len(parse_sitemap(data, max_entries=3).pages) == 3
    with pytest.raises(SitemapError, match="записей"):
        parse_sitemap(data, max_entries=2)
    with pytest.raises(SitemapError, match="байт"):
        parse_sitemap(data, max_bytes=len(data) - 1)
    bomb = gzip.compress(b" " * 10_000)
    with pytest.raises(SitemapError, match="распаковывается"):
        parse_sitemap(bomb, max_bytes=1_000)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("2026-09-01", datetime(2026, 9, 1, tzinfo=UTC)),
        ("2026-09", datetime(2026, 9, 1, tzinfo=UTC)),
        ("2026", datetime(2026, 1, 1, tzinfo=UTC)),
        ("2026-09-01T10:00Z", datetime(2026, 9, 1, 10, tzinfo=UTC)),
        (
            "2026-09-01T10:00:00.5+03:00",
            datetime(2026, 9, 1, 10, 0, 0, 500_000, tzinfo=timezone(timedelta(hours=3))),
        ),
        ("вчера", None),
        ("2026-13-01", None),
    ],
)
def test_parse_lastmod(value: str, expected: datetime | None) -> None:
    parsed = parse_lastmod(value)
    assert parsed == expected
    assert parsed is None or parsed.tzinfo is UTC
