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
# Слово длиннее — несколько «токенов»: блок без пробелов (base64, мусор вёрстки) иначе был бы
# одним словом и чанком больше лимита эмбеддера (REVIEW.md: K-16). Чанк — до max_tokens * 25
# символов: ASCII-мусор укладывается в лимит, слитный CJK-текст (≈ токен на символ) — нет.
WORD_CHARS = 25


def word_tokens(text: str) -> int:
    """Размер в «токенах» — число слов, слово длиннее WORD_CHARS — по WORD_CHARS символов на
    токен (не `memory.estimate_tokens`: та считает по символам); точный подсчёт придёт
    с токенизатором эмбеддингов (P4-06)."""
    return sum(_cost(word) for word in text.split())


def _cost(word: str) -> int:
    return -(-len(word) // WORD_CHARS)


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
    - Абзацы секции склеиваются в чанк до `max_tokens`; абзац длиннее лимита режется по словам,
      слово длиннее всего лимита — по символам.
    - Следующий чанк той же секции начинается с последних слов предыдущего на `overlap_tokens`,
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
                        token_count=word_tokens(piece),
                        section=section.path,
                    )
                )
        return drafts

    def _pack(self, blocks: list[str]) -> list[str]:
        pieces: list[str] = []
        current = ""
        for block in self._fit(blocks):
            candidate = f"{current}\n\n{block}" if current else block
            if word_tokens(candidate) <= self.max_tokens:
                current = candidate
                continue
            pieces.append(current)
            tail = self._tail(current)
            with_overlap = f"{tail}\n\n{block}" if tail else block
            current = with_overlap if word_tokens(with_overlap) <= self.max_tokens else block
        if current:
            pieces.append(current)
        return pieces

    def _fit(self, blocks: list[str]) -> list[str]:
        """Абзацы длиннее `max_tokens` — окнами по словам с перекрытием `overlap_tokens`."""
        result: list[str] = []
        for block in blocks:
            if word_tokens(block) <= self.max_tokens:
                result.append(block)
                continue
            words = self._words(block)
            costs = [_cost(w) for w in words]
            start = 0
            while True:
                end, size = start, 0
                while end < len(words) and size + costs[end] <= self.max_tokens:
                    size += costs[end]
                    end += 1
                result.append(" ".join(words[start:end]))
                if end == len(words):
                    break
                # Следующее окно — с последних слов этого в пределах перекрытия, но с продвижением.
                back, overlap = end, 0
                while back > start + 1 and overlap + costs[back - 1] <= self.overlap_tokens:
                    back -= 1
                    overlap += costs[back]
                start = back
        return result

    def _words(self, block: str) -> list[str]:
        """Слова абзаца; слово дороже всего чанка режется на куски по `max_tokens`."""
        piece = self.max_tokens * WORD_CHARS
        return [word[i : i + piece] for word in block.split() for i in range(0, len(word), piece)]

    def _tail(self, text: str) -> str:
        if self.overlap_tokens == 0:
            return ""
        words, size = text.split(), 0
        start = len(words)
        while start > 0 and size + _cost(words[start - 1]) <= self.overlap_tokens:
            start -= 1
            size += _cost(words[start])
        return " ".join(words[start:])


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
