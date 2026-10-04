"""Разбор файлов источника `file` в markdown-текст (заголовки — `#`) для структурного чанкинга."""

import re
import zipfile
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from io import BytesIO

import docx
from docx.opc.exceptions import OpcError
from docx.table import Table
from docx.text.paragraph import Paragraph
from pypdf import PdfReader
from pypdf.errors import DependencyError, PyPdfError


@dataclass(frozen=True, slots=True)
class ParsedFile:
    text: str
    title: str | None = None


type FileParser = Callable[[bytes], ParsedFile]


class FileParseError(Exception):
    """Файл повреждён, зашифрован или не содержит извлекаемого текста."""


DEFAULT_MAX_UNPACKED_BYTES = 200 * 1024 * 1024

_H1 = re.compile(r"^#\s+(.+?)(?:\s+#+)?\s*$", re.MULTILINE)
# Строка текста, которую чанкер принял бы за заголовок или code fence.
_MARKUP_START = re.compile(r"^(\s*)(#|```|~~~)", re.MULTILINE)
_HYPHEN_BREAK = re.compile(r"(\w)-\n(?=[a-zа-яё])")
_HEADING_STYLE = re.compile(r"^Heading (\d)$")


def decode_text(data: bytes) -> str:
    """UTF-8 (с BOM или без), иначе cp1251 — типичная кодировка русских .txt из Windows."""
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = data.decode("cp1251", errors="replace")
    return text.replace("\r\n", "\n").replace("\r", "\n")


def escape_markdown(text: str) -> str:
    """Экранирует `#` и ``` в начале строк: это текст, а не разметка."""
    return _MARKUP_START.sub(r"\1\\\2", text)


def parse_text(data: bytes) -> ParsedFile:
    return ParsedFile(text=decode_text(data))


def parse_markdown(data: bytes) -> ParsedFile:
    text = decode_text(data)
    match = _H1.search(text)
    return ParsedFile(text=text, title=match.group(1).strip() if match else None)


def parse_pdf(data: bytes) -> ParsedFile:
    """Текст постранично, страницы — отдельные абзацы. Заголовки PDF не распознаются."""
    try:
        reader = PdfReader(BytesIO(data))
        if reader.is_encrypted and not reader.decrypt(""):
            raise FileParseError("PDF зашифрован паролем")
        pages = [_clean_pdf_text(page.extract_text()) for page in reader.pages]
        title = reader.metadata.title if reader.metadata else None
    except (PyPdfError, DependencyError) as e:
        raise FileParseError(f"не удалось прочитать PDF: {e}") from e
    text = "\n\n".join(p for p in pages if p)
    if not text:
        raise FileParseError("в PDF нет текстового слоя (скан без OCR)")
    return ParsedFile(text=escape_markdown(text), title=(title or "").strip() or None)


def _clean_pdf_text(text: str) -> str:
    lines = [line.strip() for line in text.replace("\r\n", "\n").split("\n")]
    joined = _HYPHEN_BREAK.sub(r"\1", "\n".join(lines))
    return re.sub(r"\n{3,}", "\n\n", joined).strip()


def parse_docx(data: bytes, max_unpacked_bytes: int = DEFAULT_MAX_UNPACKED_BYTES) -> ParsedFile:
    """Заголовки `Heading N`/`Title` → `#`, списки → `- `, таблицы → markdown-таблицы."""
    try:
        with zipfile.ZipFile(BytesIO(data)) as archive:
            unpacked = sum(info.file_size for info in archive.infolist())
        if unpacked > max_unpacked_bytes:
            raise FileParseError(f"DOCX распаковывается больше чем в {max_unpacked_bytes} байт")
        document = docx.Document(BytesIO(data))
        blocks = list(_docx_blocks(document.iter_inner_content()))
        title = (document.core_properties.title or "").strip() or None
    # lxml.etree.XMLSyntaxError — подкласс SyntaxError (у lxml нет стабов для mypy).
    except (zipfile.BadZipFile, OpcError, KeyError, ValueError, SyntaxError) as e:
        raise FileParseError(f"не удалось прочитать DOCX: {e}") from e
    if title is None:
        title = next((b[2:] for b in blocks if b.startswith("# ")), None)
    return ParsedFile(text="\n\n".join(blocks), title=title)


def _docx_blocks(content: Iterator[Paragraph | Table]) -> Iterator[str]:
    for item in content:
        if isinstance(item, Table):
            table = _docx_table(item)
            if table:
                yield table
            continue
        text = item.text.strip()
        if not text:
            continue
        level = _heading_level(item)
        if level is not None:
            yield f"{'#' * level} {' '.join(text.split())}"
        elif _is_list_item(item):
            yield "- " + escape_markdown(text).replace("\n", "\n  ")
        else:
            yield escape_markdown(text)


def _heading_level(paragraph: Paragraph) -> int | None:
    name = paragraph.style.name if paragraph.style is not None else None
    if name == "Title":
        return 1
    match = _HEADING_STYLE.match(name or "")
    return min(int(match.group(1)), 6) if match else None


def _is_list_item(paragraph: Paragraph) -> bool:
    name = paragraph.style.name if paragraph.style is not None else None
    p_pr = paragraph._p.pPr
    return (name or "").startswith("List") or (p_pr is not None and p_pr.numPr is not None)


def _docx_table(table: Table) -> str:
    rows = [
        [" ".join(cell.text.split()).replace("|", "\\|") for cell in row.cells]
        for row in table.rows
    ]
    rows = [r for r in rows if any(r)]
    if not rows:
        return ""
    width = max(len(r) for r in rows)
    rows = [r + [""] * (width - len(r)) for r in rows]
    lines = [_table_row(rows[0]), _table_row(["---"] * width)]
    lines += [_table_row(r) for r in rows[1:]]
    return "\n".join(lines)


def _table_row(cells: list[str]) -> str:
    return "| " + " | ".join(cells) + " |"


PARSERS: dict[str, FileParser] = {
    ".md": parse_markdown,
    ".markdown": parse_markdown,
    ".txt": parse_text,
    ".pdf": parse_pdf,
    ".docx": parse_docx,
}
