"""Структурный чанкинг markdown-текста по заголовкам (architecture.md §8).

Коннекторы приводят документы к markdown (`#` — заголовки), поэтому чанкер общий
для всех форматов.
"""

import re
from dataclasses import dataclass, field

from app.modules.knowledge.domain.ingestion import ChunkDraft

_HEADING = re.compile(r"^(#{1,6})\s+(.+?)(?:\s+#+)?\s*$")
_FENCE = re.compile(r"^\s*(```|~~~)")
SECTION_SEPARATOR = " > "


def estimate_tokens(text: str) -> int:
    """Приблизительно — число слов; точный подсчёт придёт с токенизатором эмбеддингов (P4-06)."""
    return len(text.split())


@dataclass(slots=True)
class _Section:
    path: str | None
    blocks: list[str] = field(default_factory=list)
    has_body: bool = False


class MarkdownChunker:
    """Режет текст на секции по заголовкам `#`…`######` (вне code fences), секцию — на чанки.

    - Чанк не пересекает границу секции; `section` — путь заголовков («Доставка > Сроки»),
      текст до первого заголовка — секция `None`. Секция без текста (только заголовок)
      пропускается, её заголовок остаётся в пути вложенных секций.
    - Абзацы секции склеиваются в чанк до `max_tokens`; абзац длиннее лимита режется по словам.
    - Следующий чанк той же секции начинается с последних `overlap_tokens` слов предыдущего,
      если это не выводит его за `max_tokens`.
    """

    def __init__(self, max_tokens: int = 500, overlap_tokens: int = 50) -> None:
        if not 0 <= overlap_tokens < max_tokens:
            raise ValueError("нужно 0 <= overlap_tokens < max_tokens")
        self.max_tokens = max_tokens
        self.overlap_tokens = overlap_tokens

    def chunk(self, text: str) -> list[ChunkDraft]:
        drafts: list[ChunkDraft] = []
        for section in _sections(text):
            if not section.has_body:
                continue
            for piece in self._pack(section.blocks):
                drafts.append(
                    ChunkDraft(
                        ord=len(drafts),
                        text=piece,
                        token_count=estimate_tokens(piece),
                        section=section.path,
                    )
                )
        return drafts

    def _pack(self, blocks: list[str]) -> list[str]:
        pieces: list[str] = []
        current = ""
        for block in self._fit(blocks):
            candidate = f"{current}\n\n{block}" if current else block
            if estimate_tokens(candidate) <= self.max_tokens:
                current = candidate
                continue
            pieces.append(current)
            tail = self._tail(current)
            with_overlap = f"{tail}\n\n{block}" if tail else block
            current = with_overlap if estimate_tokens(with_overlap) <= self.max_tokens else block
        if current:
            pieces.append(current)
        return pieces

    def _fit(self, blocks: list[str]) -> list[str]:
        """Абзацы длиннее `max_tokens` — окнами по словам с перекрытием."""
        result: list[str] = []
        step = self.max_tokens - self.overlap_tokens
        for block in blocks:
            words = block.split()
            if len(words) <= self.max_tokens:
                result.append(block)
                continue
            for start in range(0, len(words) - self.overlap_tokens, step):
                result.append(" ".join(words[start : start + self.max_tokens]))
        return result

    def _tail(self, text: str) -> str:
        if self.overlap_tokens == 0:
            return ""
        return " ".join(text.split()[-self.overlap_tokens :])


def _sections(text: str) -> list[_Section]:
    sections = [_Section(path=None)]
    headings: list[tuple[int, str]] = []
    paragraph: list[str] = []
    in_fence = False

    def flush() -> None:
        block = "\n".join(paragraph).strip()
        paragraph.clear()
        if block:
            sections[-1].blocks.append(block)

    for line in text.splitlines():
        if _FENCE.match(line):
            in_fence = not in_fence
        elif not in_fence and (match := _HEADING.match(line)):
            flush()
            level, title = len(match.group(1)), match.group(2).strip()
            headings = [(lvl, t) for lvl, t in headings if lvl < level]
            headings.append((level, title))
            sections.append(_Section(path=SECTION_SEPARATOR.join(t for _, t in headings)))
            sections[-1].blocks.append(line.strip())
            continue
        elif not in_fence and not line.strip():
            flush()
            continue
        paragraph.append(line)
        sections[-1].has_body = sections[-1].has_body or bool(line.strip())
    flush()
    return sections
