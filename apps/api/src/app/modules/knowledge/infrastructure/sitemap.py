"""Разбор sitemap (sitemaps.org): `urlset`, `sitemapindex`, текстовый список URL, gzip.

Только разбор байтов — скачивание, обход вложенных sitemap и лимиты на их число делает
коннектор `website`. Битый XML — `SitemapError`; битая отдельная запись (не URL, чужая
схема) пропускается.
"""

import re
import zlib
from dataclasses import dataclass
from datetime import UTC, datetime
from xml.etree import ElementTree

from app.modules.knowledge.infrastructure.web_urls import normalize_url

# Лимиты протокола sitemaps.org для одного файла.
DEFAULT_MAX_BYTES = 50 * 1024 * 1024
DEFAULT_MAX_ENTRIES = 50_000

_GZIP_MAGIC = b"\x1f\x8b"
_LASTMOD_PARTIAL = re.compile(r"^(\d{4})(?:-(\d{2}))?$")


class SitemapError(Exception):
    """Sitemap не читается: не XML/текст, не тот корень, превышены лимиты."""


@dataclass(frozen=True, slots=True)
class SitemapEntry:
    url: str
    lastmod: datetime | None = None


@dataclass(frozen=True, slots=True)
class Sitemap:
    """`pages` — страницы (`urlset` или текст), `sitemaps` — вложенные (`sitemapindex`)."""

    pages: list[SitemapEntry]
    sitemaps: list[SitemapEntry]


def parse_sitemap(
    data: bytes,
    *,
    max_bytes: int = DEFAULT_MAX_BYTES,
    max_entries: int = DEFAULT_MAX_ENTRIES,
) -> Sitemap:
    """Записи с одинаковым URL схлопываются (остаётся первая)."""
    if data.startswith(_GZIP_MAGIC):
        data = _gunzip(data, max_bytes)
    if len(data) > max_bytes:
        raise SitemapError(f"sitemap больше {max_bytes} байт")
    stripped = data.lstrip(b"\xef\xbb\xbf \t\r\n")
    entries, nested = _parse_xml(stripped) if stripped.startswith(b"<") else _parse_text(stripped)
    if len(entries) > max_entries:
        raise SitemapError(f"в sitemap больше {max_entries} записей")
    unique = _unique(entries)
    return Sitemap(pages=[] if nested else unique, sitemaps=unique if nested else [])


def parse_lastmod(value: str) -> datetime | None:
    """W3C Datetime (`2024-01-15`, `2024-01-15T10:00:00+03:00`, `…Z`, `2024-01`) → aware UTC."""
    value = value.strip()
    partial = _LASTMOD_PARTIAL.match(value)
    try:
        if partial:
            parsed = datetime(int(partial.group(1)), int(partial.group(2) or 1), 1)
        else:
            parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _gunzip(data: bytes, max_bytes: int) -> bytes:
    decompressor = zlib.decompressobj(wbits=16 + zlib.MAX_WBITS)
    try:
        unpacked = decompressor.decompress(data, max_bytes + 1)
    except zlib.error as e:
        raise SitemapError(f"битый gzip: {e}") from e
    if len(unpacked) > max_bytes:
        raise SitemapError(f"sitemap распаковывается больше чем в {max_bytes} байт")
    return unpacked


def _parse_xml(data: bytes) -> tuple[list[SitemapEntry], bool]:
    # Sitemap не нуждается в DTD; сущности — путь к XML-бомбам, их не принимаем вовсе.
    if b"<!ENTITY" in data or b"<!DOCTYPE" in data:
        raise SitemapError("DTD в sitemap не поддерживается")
    try:
        root = ElementTree.fromstring(data)
    except ElementTree.ParseError as e:
        raise SitemapError(f"битый XML: {e}") from e
    kind = _local_name(root.tag)
    if kind not in ("urlset", "sitemapindex"):
        raise SitemapError(f"неизвестный корень sitemap: <{kind}>")
    item_tag = "url" if kind == "urlset" else "sitemap"
    entries: list[SitemapEntry] = []
    for item in root:
        if _local_name(item.tag) != item_tag:
            continue
        fields = {_local_name(child.tag): (child.text or "").strip() for child in item}
        url = normalize_url(fields.get("loc", ""))
        if url is None:
            continue
        lastmod = fields.get("lastmod")
        entries.append(SitemapEntry(url, parse_lastmod(lastmod) if lastmod else None))
    return entries, kind == "sitemapindex"


def _parse_text(data: bytes) -> tuple[list[SitemapEntry], bool]:
    """Текстовый sitemap: по URL в строке, UTF-8."""
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as e:
        raise SitemapError("текстовый sitemap не в UTF-8") from e
    entries = []
    for line in text.splitlines():
        url = normalize_url(line) if line.strip() else None
        if url is not None:
            entries.append(SitemapEntry(url))
    if not entries and text.strip():
        raise SitemapError("sitemap не XML и не список URL")
    return entries, False


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _unique(entries: list[SitemapEntry]) -> list[SitemapEntry]:
    seen: dict[str, SitemapEntry] = {}
    for entry in entries:
        seen.setdefault(entry.url, entry)
    return list(seen.values())
