"""Простой чанкер по абзацам. Структурный чанкинг по заголовкам — P4-03."""

import re

from app.modules.knowledge.domain.ingestion import ChunkDraft

_PARAGRAPH_BREAK = re.compile(r"\n\s*\n")


class ParagraphChunker:
    """Склеивает абзацы в чанки до `max_chars`; абзац длиннее лимита режется по символам.

    `token_count` приблизительный (число слов): точный подсчёт появится вместе с
    токенизатором эмбеддингов (P4-06).
    """

    def __init__(self, max_chars: int = 2000) -> None:
        self.max_chars = max_chars

    def chunk(self, text: str) -> list[ChunkDraft]:
        pieces: list[str] = []
        current = ""
        for paragraph in self._paragraphs(text):
            candidate = f"{current}\n\n{paragraph}" if current else paragraph
            if len(candidate) <= self.max_chars:
                current = candidate
                continue
            if current:
                pieces.append(current)
            current = paragraph
        if current:
            pieces.append(current)
        return [
            ChunkDraft(ord=i, text=piece, token_count=len(piece.split()))
            for i, piece in enumerate(pieces)
        ]

    def _paragraphs(self, text: str) -> list[str]:
        result: list[str] = []
        for paragraph in _PARAGRAPH_BREAK.split(text):
            paragraph = paragraph.strip()
            for start in range(0, len(paragraph), self.max_chars):
                result.append(paragraph[start : start + self.max_chars])
        return result
