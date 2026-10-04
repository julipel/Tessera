"""Разбор файлов источника `file` в markdown-текст (заголовки — `#`) для структурного чанкинга."""

import re
from collections.abc import Callable
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ParsedFile:
    text: str
    title: str | None = None


type FileParser = Callable[[bytes], ParsedFile]

_H1 = re.compile(r"^#\s+(.+?)(?:\s+#+)?\s*$", re.MULTILINE)


def decode_text(data: bytes) -> str:
    """UTF-8 (с BOM или без), иначе cp1251 — типичная кодировка русских .txt из Windows."""
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = data.decode("cp1251", errors="replace")
    return text.replace("\r\n", "\n").replace("\r", "\n")


def parse_text(data: bytes) -> ParsedFile:
    return ParsedFile(text=decode_text(data))


def parse_markdown(data: bytes) -> ParsedFile:
    text = decode_text(data)
    match = _H1.search(text)
    return ParsedFile(text=text, title=match.group(1).strip() if match else None)


PARSERS: dict[str, FileParser] = {
    ".md": parse_markdown,
    ".markdown": parse_markdown,
    ".txt": parse_text,
}
