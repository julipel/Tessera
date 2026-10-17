"""Структурный чанкинг markdown по заголовкам."""

import pytest

from app.modules.knowledge.application.chunking import WORD_CHARS, word_tokens
from app.modules.knowledge.public import MarkdownChunker


def sections(chunker: MarkdownChunker, text: str) -> list[tuple[str | None, str]]:
    return [(c.section, c.text) for c in chunker.chunk(text)]


def test_splits_by_headings_with_section_path() -> None:
    text = (
        "Вступление.\n\n"
        "# Доставка\n\n"
        "Курьером по Москве.\n\n"
        "## Сроки\n\n"
        "Два дня.\n\n"
        "## Стоимость ##\n\n"
        "Бесплатно от 3000 ₽.\n\n"
        "# Возврат\n\n"
        "14 дней."
    )

    assert sections(MarkdownChunker(), text) == [
        (None, "Вступление."),
        ("Доставка", "# Доставка\n\nКурьером по Москве."),
        ("Доставка > Сроки", "## Сроки\n\nДва дня."),
        ("Доставка > Стоимость", "## Стоимость ##\n\nБесплатно от 3000 ₽."),
        ("Возврат", "# Возврат\n\n14 дней."),
    ]


def test_heading_without_body_is_skipped_but_kept_in_path() -> None:
    text = "# Услуги\n\n## Маникюр\n\nКлассический."

    assert sections(MarkdownChunker(), text) == [
        ("Услуги > Маникюр", "## Маникюр\n\nКлассический."),
    ]


def test_headings_inside_code_fence_are_ignored() -> None:
    text = "# Настройка\n\n```bash\n# не заголовок\n\necho ok\n```\n\nГотово."

    assert sections(MarkdownChunker(), text) == [
        ("Настройка", "# Настройка\n\n```bash\n# не заголовок\n\necho ok\n```\n\nГотово."),
    ]


def test_text_without_headings_is_one_section() -> None:
    chunks = MarkdownChunker().chunk("Первый абзац.\r\n\r\nВторой абзац.")

    assert [(c.ord, c.section, c.text, c.token_count) for c in chunks] == [
        (0, None, "Первый абзац.\n\nВторой абзац.", 4),
    ]
    assert MarkdownChunker().chunk("  \n\n ") == []


def test_long_section_is_packed_by_paragraphs_with_overlap() -> None:
    chunker = MarkdownChunker(max_tokens=5, overlap_tokens=1)
    text = "# Уход\n\nраз два\n\nтри четыре\n\nпять шесть семь"

    assert sections(chunker, text) == [
        ("Уход", "# Уход\n\nраз два"),
        ("Уход", "два\n\nтри четыре"),
        ("Уход", "четыре\n\nпять шесть семь"),
    ]


def test_overlap_is_dropped_when_it_does_not_fit() -> None:
    chunker = MarkdownChunker(max_tokens=3, overlap_tokens=1)

    assert [c.text for c in chunker.chunk("раз два\n\nтри четыре пять")] == [
        "раз два",
        "три четыре пять",
    ]


def test_paragraph_longer_than_limit_is_split_by_words() -> None:
    chunker = MarkdownChunker(max_tokens=4, overlap_tokens=1)

    chunks = chunker.chunk("a b c d e f g h i j")

    assert [c.text for c in chunks] == ["a b c d", "d e f g", "g h i j"]
    assert all(c.token_count <= 4 for c in chunks)


def test_block_without_spaces_is_split_within_limit() -> None:
    # base64 без пробелов — не одно «слово» на весь документ (REVIEW.md: K-16).
    blob = "QUJD" * 10_000  # 40 000 символов
    chunker = MarkdownChunker(max_tokens=500, overlap_tokens=50)

    chunks = chunker.chunk(f"Фото товара:\n\n{blob}")

    assert len(chunks) > 1
    assert all(c.token_count <= 500 for c in chunks)
    assert all(len(c.text) <= 500 * WORD_CHARS for c in chunks)
    # Ничего не потеряно и не задвоено: кусок дороже перекрытия, поэтому без перекрытия.
    pieces = [c.text for c in chunks if "Фото" not in c.text]
    assert "".join(pieces) == blob


def test_overlap_counts_cost_of_long_words() -> None:
    long = "x" * 50  # 2 «токена»
    chunker = MarkdownChunker(max_tokens=4, overlap_tokens=2)

    # Перекрытие — последнее слово в 2 токена, а не два последних слова.
    assert [c.text for c in chunker.chunk(f"a b {long} c d e")] == [
        f"a b {long}",
        f"{long} c d",
        "c d e",
    ]
    # Последнее слово дороже перекрытия — следующее окно начинается без него.
    big = "y" * 75  # 3 «токена»
    assert [c.text for c in chunker.chunk(f"a {big} b c d")] == [f"a {big}", "b c d"]


def test_long_word_costs_more_but_stays_whole() -> None:
    url = "https://example.ru/" + "a" * 81  # 100 символов — 4 «токена»
    chunks = MarkdownChunker(max_tokens=6, overlap_tokens=0).chunk(f"см. {url} и ещё")

    assert word_tokens(url) == 4
    assert [c.text for c in chunks] == [f"см. {url} и", "ещё"]


def test_chunks_never_cross_sections() -> None:
    chunks = MarkdownChunker(max_tokens=100).chunk("# А\n\nпервый\n\n# Б\n\nвторой")

    assert [c.section for c in chunks] == ["А", "Б"]
    assert [c.ord for c in chunks] == [0, 1]


def test_invalid_limits() -> None:
    with pytest.raises(ValueError):
        MarkdownChunker(max_tokens=10, overlap_tokens=10)
