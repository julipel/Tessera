"""Извлечение основного контента HTML для коннектора `website`: корень, разметка, ссылки."""

import pytest

from app.modules.knowledge.infrastructure.html_extract import (
    MAX_DEPTH,
    decode_html,
    extract_page,
)
from app.modules.knowledge.public import MarkdownChunker

URL = "https://example.ru/help/delivery"


def page(body: str, head: str = "") -> str:
    return f"<!doctype html><html><head>{head}</head><body>{body}</body></html>"


def test_main_content_with_headings_lists_tables_and_code() -> None:
    html = page(
        """
        <header><a href="/">Лого</a></header>
        <nav><a href="/catalog">Каталог</a></nav>
        <main>
          <h1>Доставка</h1>
          <p>Доставляем по <b>всей</b> России.<br>Срок&nbsp;— 3 дня.
          <h2>Стоимость</h2>
          <ul><li>Москва — 300 ₽<li>Регионы<ul><li>до 5 кг — 500 ₽</ul></ul>
          <ol><li>Оформите<li><p>Оплатите</p></ol>
          <table><tr><th>Зона<th>Цена<tr><td>A | B<td>100</table>
          <pre>x = 1
  y = 2</pre>
          <aside>Реклама</aside>
          <footer>Поделиться</footer>
        </main>
        <footer>© 2026</footer>
        """,
        head="<title> Доставка | Магазин </title><script>var a = '<p>x</p>';</script>",
    )
    result = extract_page(html, URL)

    assert result.title == "Доставка | Магазин"
    assert result.text == (
        "# Доставка\n\n"
        "Доставляем по всей России.\nСрок — 3 дня.\n\n"
        "## Стоимость\n\n"
        "- Москва — 300 ₽\n- Регионы\n  - до 5 кг — 500 ₽\n\n"
        "1. Оформите\n2. Оплатите\n\n"
        "| Зона | Цена |\n| --- | --- |\n| A \\| B | 100 |\n\n"
        "```\nx = 1\n  y = 2\n```"
    )


def test_markdown_feeds_structural_chunker() -> None:
    html = page("<main><h1>Доставка</h1><h2>Сроки</h2><p>Три дня.</p></main>")
    chunks = MarkdownChunker().chunk(extract_page(html, URL).text)
    assert [(c.section, c.text) for c in chunks] == [("Доставка > Сроки", "## Сроки\n\nТри дня.")]


def test_body_fallback_skips_site_chrome() -> None:
    html = page(
        """
        <header><h1>Магазин</h1></header>
        <div role="navigation">Меню</div>
        <div class="content"><h1>Оплата</h1><p>Картой или наличными.</p></div>
        <div role="contentinfo">Контакты</div>
        <div hidden>Скрыто</div><span aria-hidden="true">★</span>
        <form><label>Поиск</label><input name="q"><button>Найти</button></form>
        """
    )
    assert extract_page(html, URL).text == "# Оплата\n\nКартой или наличными.\n\nПоиск"


def test_header_inside_main_is_content() -> None:
    html = page("<main><header><h1>Возврат</h1></header><p>14 дней.</p></main>")
    assert extract_page(html, URL).text == "# Возврат\n\n14 дней."


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        ('<div>Шапка</div><div role="main"><p>Тело</p></div>', "Тело"),
        ("<div>Шапка</div><article><p>Статья</p></article>", "Статья"),
        # Несколько статей — это лента, а не одна статья: берём всю страницу.
        ("<article><p>Первая</p></article><article><p>Вторая</p></article>", "Первая\n\nВторая"),
        # Пустой main (оболочка SPA) не годится в корень.
        ("<main></main><div><p>Текст</p></div>", "Текст"),
    ],
)
def test_content_root_selection(body: str, expected: str) -> None:
    assert extract_page(page(body), URL).text == expected


def test_title_falls_back_to_first_h1() -> None:
    assert extract_page(page("<main><p>a</p><h1>Контакты</h1></main>"), URL).title == "Контакты"
    assert extract_page(page("<p>Без заголовка</p>"), URL).title is None


def test_text_that_looks_like_markup_is_escaped() -> None:
    html = page("<main><p># не заголовок</p><p>```не код</p></main>")
    assert extract_page(html, URL).text == "\\# не заголовок\n\n\\```не код"


def test_pre_with_backticks_uses_tilde_fence() -> None:
    html = page("<main><pre>\n```bash\nls\n```\n</pre></main>")
    assert extract_page(html, URL).text == "~~~\n```bash\nls\n```\n~~~"


def test_layout_table_is_rendered_as_blocks() -> None:
    assert extract_page(page("<table><tr><td>Один</td><td>ряд</td></tr></table>"), URL).text == (
        "Один\n\nряд"
    )
    html = page(
        "<table><tr><td><table><tr><td>Меню</td></tr></table></td>"
        "<td><h1>О компании</h1><p>Ведём дела давно.</p></td></tr></table>"
    )
    assert extract_page(html, URL).text == "Меню\n\n# О компании\n\nВедём дела давно."


def test_tolerates_broken_markup() -> None:
    html = (
        "<body><div><p>Первый<p>Второй</div></span></b>"
        "<ul><li>a<li>b</ul><table><tr><td>1<td>2<tr><td>3<td>4</table>"
        "<dl><dt>Срок<dd>3 дня</dl><p>Без закрытия"
    )
    assert extract_page(html, URL).text == (
        "Первый\n\nВторой\n\n- a\n- b\n\n| 1 | 2 |\n| --- | --- |\n| 3 | 4 |\n\n"
        "Срок\n\n3 дня\n\nБез закрытия"
    )


def test_deep_nesting_does_not_blow_the_stack() -> None:
    depth = MAX_DEPTH * 10
    html = page("<div>" * depth + "<p>Глубоко</p>" + "</div>" * depth)
    assert extract_page(html, URL).text == "Глубоко"


def test_links_are_absolute_normalized_and_unique() -> None:
    html = page(
        """
        <nav><a href="/catalog">Каталог</a><a href="../about#team">О нас</a></nav>
        <main>
          <a href="HTTPS://Example.RU:443/catalog">Каталог ещё раз</a>
          <a href="mailto:shop@example.ru">почта</a> <a href="javascript:void(0)">x</a>
          <a href="tel:+7900">звонок</a> <a href="/login" rel="NoFollow">Вход</a>
          <a href="https://other.ru/x?a=1">Партнёр</a> <a>без href</a>
        </main>
        <map><area href="/map"></map>
        """
    )
    assert extract_page(html, URL).links == [
        "https://example.ru/catalog",
        "https://example.ru/about",
        "https://other.ru/x?a=1",
        "https://example.ru/map",
    ]


def test_base_href_changes_link_resolution() -> None:
    html = page('<a href="price">Цены</a>', head='<base href="https://example.ru/shop/">')
    assert extract_page(html, URL).links == ["https://example.ru/shop/price"]


@pytest.mark.parametrize(
    ("content", "noindex", "nofollow"),
    [
        ("index, follow", False, False),
        ("NOINDEX", True, False),
        ("noindex, nofollow", True, True),
        ("none", True, True),
    ],
)
def test_meta_robots(content: str, noindex: bool, nofollow: bool) -> None:
    html = page('<a href="/x">x</a>', head=f'<meta name="Robots" content="{content}">')
    result = extract_page(html, URL)
    assert (result.noindex, result.nofollow) == (noindex, nofollow)
    assert result.links == ([] if nofollow else ["https://example.ru/x"])


@pytest.mark.parametrize(
    ("data", "charset", "expected"),
    [
        ("Привет".encode("cp1251"), "windows-1251", "Привет"),
        (b'<meta charset="windows-1251">' + "Привет".encode("cp1251"), None, None),
        (
            b'<meta http-equiv="Content-Type" content="text/html; charset=koi8-r">'
            + "Привет".encode("koi8-r"),
            None,
            None,
        ),
        ("﻿Привет".encode(), "windows-1251", "Привет"),  # BOM важнее заголовка
        ("Привет".encode(), "no-such-charset", "Привет"),
        ("Привет".encode("cp1251"), None, "Привет"),  # не UTF-8 — cp1251
    ],
)
def test_decode_html(data: bytes, charset: str | None, expected: str | None) -> None:
    decoded = decode_html(data, charset)
    assert decoded == expected if expected is not None else decoded.endswith("Привет")
