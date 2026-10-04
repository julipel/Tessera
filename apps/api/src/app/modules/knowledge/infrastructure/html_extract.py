"""Основной контент HTML-страницы в markdown (заголовки — `#`) для коннектора `website`.

Разбор — stdlib `html.parser` с терпимым построителем дерева: неявное закрытие `p`, `li`,
`td`… и пропуск лишних закрывающих тегов, как делают браузеры (упрощённо).

Корень контента — первый непустой `<main>`/`[role=main]`, иначе единственный `<article>`,
иначе `<body>`. Внутри корня не выводятся навигация, сайдбары, футеры, скрипты, формы
ввода, скрытые элементы; шапка (`<header>`, `role=banner`) — только если корень `<body>`
(внутри `<main>`/`<article>` в шапке обычно заголовок страницы).

Ссылки для обхода собираются со всей страницы, включая навигацию.
"""

import codecs
import re
from collections.abc import Iterator
from dataclasses import dataclass, field
from html.parser import HTMLParser

from app.modules.knowledge.infrastructure.file_parsers import escape_markdown, markdown_table
from app.modules.knowledge.infrastructure.web_urls import normalize_url

# Глубже элементы не вкладываются (их текст достаётся родителю): рендер рекурсивный.
MAX_DEPTH = 200

_VOID = frozenset(
    {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param",
     "source", "track", "wbr"}
)  # fmt: skip
_HEADINGS = {f"h{n}": n for n in range(1, 7)}
_INLINE = frozenset(
    {"a", "abbr", "b", "bdi", "bdo", "cite", "code", "data", "del", "dfn", "em", "font", "i",
     "img", "ins", "kbd", "label", "mark", "q", "s", "samp", "small", "span", "strong", "sub",
     "sup", "time", "tt", "u", "var", "wbr"}
)  # fmt: skip
_SKIP = frozenset(
    {"aside", "button", "canvas", "dialog", "embed", "footer", "head", "iframe", "input",
     "math", "nav", "noscript", "object", "script", "select", "style", "svg", "template",
     "textarea", "title"}
)  # fmt: skip
_SKIP_ROLES = frozenset(
    {"complementary", "contentinfo", "dialog", "menu", "menubar", "navigation", "search"}
)
_LAYOUT_MARKERS = frozenset({"table", "main", "article", "section", "form", *_HEADINGS})

# Новый тег закрывает открытый элемент из `closes`, если между ними нет элемента из `scope`.
_P_SCOPE = frozenset({"button", "caption", "html", "object", "table", "td", "template", "th"})
_P_CLOSERS = frozenset(
    {"address", "article", "aside", "blockquote", "details", "div", "dl", "fieldset",
     "figcaption", "figure", "footer", "form", "header", "hgroup", "hr", "main", "menu", "nav",
     "ol", "p", "pre", "section", "table", "ul", *_HEADINGS}
)  # fmt: skip
_IMPLIED_END: dict[str, tuple[frozenset[str], frozenset[str]]] = {
    **{tag: (frozenset({"p"}), _P_SCOPE) for tag in _P_CLOSERS},
    "li": (frozenset({"li"}), frozenset({"ul", "ol", "table", "td", "th"})),
    "dt": (frozenset({"dt", "dd"}), frozenset({"dl", "table"})),
    "dd": (frozenset({"dt", "dd"}), frozenset({"dl", "table"})),
    "tr": (frozenset({"tr"}), frozenset({"table"})),
    "td": (frozenset({"td", "th"}), frozenset({"tr", "table"})),
    "th": (frozenset({"td", "th"}), frozenset({"tr", "table"})),
    "thead": (frozenset({"thead", "tbody", "tfoot"}), frozenset({"table"})),
    "tbody": (frozenset({"thead", "tbody", "tfoot"}), frozenset({"table"})),
    "tfoot": (frozenset({"thead", "tbody", "tfoot"}), frozenset({"table"})),
    "option": (frozenset({"option"}), frozenset({"select"})),
}

_META_CHARSET = re.compile(rb"""<meta[^>]+charset\s*=\s*["']?\s*([A-Za-z0-9_.:-]+)""", re.I)


@dataclass(frozen=True, slots=True)
class ExtractedPage:
    """`text` — markdown основного контента (может быть пустым); `links` — абсолютные
    нормализованные http(s)-ссылки страницы без дублей (пусто при `nofollow`)."""

    title: str | None
    text: str
    links: list[str]
    noindex: bool = False
    nofollow: bool = False


def decode_html(data: bytes, charset: str | None = None) -> str:
    """BOM → `charset` (из Content-Type) → `<meta charset>` в начале → UTF-8, иначе cp1251."""
    if data.startswith(codecs.BOM_UTF8):
        return data[len(codecs.BOM_UTF8) :].decode("utf-8", errors="replace")
    meta = _META_CHARSET.search(data[:4096])
    for name in (charset, meta.group(1).decode("ascii") if meta else None):
        if not name:
            continue
        try:
            return data.decode(name, errors="replace")
        except LookupError:
            continue
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return data.decode("cp1251", errors="replace")


def extract_page(html: str, url: str) -> ExtractedPage:
    """`url` — адрес страницы: от него (или от `<base href>`) разрешаются ссылки."""
    builder = _TreeBuilder()
    builder.feed(html)
    builder.close()
    document = builder.root
    elements = list(_iter_elements(document))

    robots: set[str] = set()
    base = url
    title: str | None = None
    for element in elements:
        if element.tag == "meta" and element.attrs.get("name", "").lower() == "robots":
            robots |= {t.strip().lower() for t in element.attrs.get("content", "").split(",")}
        elif element.tag == "base" and base is url and "href" in element.attrs:
            base = normalize_url(element.attrs["href"], url) or url
        elif element.tag == "title" and title is None:
            title = _collapse(_raw_text(element)) or None
    noindex = bool(robots & {"noindex", "none"})
    nofollow = bool(robots & {"nofollow", "none"})

    root = _content_root(elements, document)
    renderer = _Renderer(skip_header=root.tag in ("body", "#document"))
    renderer.render(root)
    if title is None:
        title = next((b[2:] for b in renderer.blocks if b.startswith("# ")), None)
    return ExtractedPage(
        title=title,
        text="\n\n".join(renderer.blocks),
        links=[] if nofollow else _links(elements, base),
        noindex=noindex,
        nofollow=nofollow,
    )


@dataclass(eq=False, slots=True)
class _Element:
    tag: str
    attrs: dict[str, str]
    children: list["_Element | str"] = field(default_factory=list)


class _TreeBuilder(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.root = _Element("#document", {})
        self.stack = [self.root]

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self._close_implied(tag)
        element = _Element(tag, {name: value or "" for name, value in reversed(attrs)})
        self.stack[-1].children.append(element)
        if tag not in _VOID and len(self.stack) < MAX_DEPTH:
            self.stack.append(element)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        # `<div/>` в HTML — открывающий тег, но авторы XHTML имеют в виду пустой элемент.
        self.handle_starttag(tag, attrs)
        if tag not in _VOID:
            self.handle_endtag(tag)

    def handle_endtag(self, tag: str) -> None:
        for i in range(len(self.stack) - 1, 0, -1):
            if self.stack[i].tag == tag:
                del self.stack[i:]
                return

    def handle_data(self, data: str) -> None:
        self.stack[-1].children.append(data)

    def _close_implied(self, tag: str) -> None:
        rule = _IMPLIED_END.get(tag)
        if rule is None:
            return
        closes, scope = rule
        for i in range(len(self.stack) - 1, 0, -1):
            current = self.stack[i].tag
            if current in closes:
                del self.stack[i:]
                return
            if current in scope:
                return


class _Renderer:
    def __init__(self, *, skip_header: bool) -> None:
        self.skip_header = skip_header
        self.blocks: list[str] = []
        self._inline: list[str] = []

    def render(self, root: _Element) -> None:
        self._children(root)
        self._flush()

    def _children(self, element: _Element) -> None:
        for child in element.children:
            self._node(child)

    def _node(self, node: "_Element | str") -> None:
        if isinstance(node, str):
            self._inline.append(node)
            return
        if self._skipped(node):
            return
        tag = node.tag
        if tag in _HEADINGS:
            self._block(f"{'#' * _HEADINGS[tag]} {self._inline_text(node)}")
        elif tag in ("ul", "ol"):
            self._block("\n".join(self._list(node, ordered=tag == "ol", depth=0)))
        elif tag == "pre":
            self._block(_fence(_raw_text(node)))
        elif tag == "table" and (rows := self._data_rows(node)):
            self._block(markdown_table(rows))
        elif tag == "br":
            self._inline.append("\n")
        elif tag in _INLINE:
            self._children(node)
        else:
            self._flush()
            self._children(node)
            self._flush()

    def _block(self, text: str) -> None:
        self._flush()
        if text.strip("# "):
            self.blocks.append(text)

    def _flush(self) -> None:
        lines = (" ".join(line.split()) for line in "".join(self._inline).split("\n"))
        self._inline.clear()
        paragraph = "\n".join(line for line in lines if line)
        if paragraph:
            self.blocks.append(escape_markdown(paragraph))

    def _skipped(self, element: _Element) -> bool:
        attrs = element.attrs
        role = attrs.get("role", "").lower()
        return (
            element.tag in _SKIP
            or role in _SKIP_ROLES
            or (self.skip_header and (element.tag == "header" or role == "banner"))
            or "hidden" in attrs
            or attrs.get("aria-hidden", "").lower() == "true"
        )

    def _inline_text(self, node: "_Element | str") -> str:
        parts: list[str] = []
        self._collect(node, parts)
        return _collapse("".join(parts))

    def _collect(self, node: "_Element | str", parts: list[str]) -> None:
        if isinstance(node, str):
            parts.append(node)
        elif node.tag == "br":
            parts.append(" ")
        elif not self._skipped(node):
            if node.tag not in _INLINE:
                parts.append(" ")  # блоки внутри пункта списка или ячейки — через пробел
            for child in node.children:
                self._collect(child, parts)

    def _list(self, element: _Element, *, ordered: bool, depth: int) -> list[str]:
        lines: list[str] = []
        number = 0
        for child in element.children:
            if isinstance(child, str) or self._skipped(child):
                continue
            if child.tag != "li":  # вложенный список или обёртка вокруг `li`
                nested_ordered = child.tag == "ol" if child.tag in ("ul", "ol") else ordered
                lines += self._list(child, ordered=nested_ordered, depth=depth)
                continue
            parts: list[str] = []
            nested: list[str] = []
            for sub in child.children:
                if isinstance(sub, _Element) and sub.tag in ("ul", "ol") and not self._skipped(sub):
                    nested += self._list(sub, ordered=sub.tag == "ol", depth=depth + 1)
                else:
                    self._collect(sub, parts)
            text = _collapse("".join(parts))
            if text:
                number += 1
                marker = f"{number}." if ordered else "-"
                lines.append(f"{'  ' * depth}{marker} {text}")
            lines += nested
        return lines

    def _data_rows(self, table: _Element) -> list[list[str]] | None:
        """Строки таблицы с данными; None — таблица-вёрстка, её выводим как блоки."""
        if _is_layout_table(table):
            return None
        rows = [r for r in self._rows(table) if any(r)]
        # Одна строка или одна колонка — не таблица, а обёртка вокруг текста.
        if len(rows) < 2 or max(len(r) for r in rows) < 2:
            return None
        return rows

    def _rows(self, table: _Element) -> list[list[str]]:
        return [
            [
                self._inline_text(cell)
                for cell in row.children
                if isinstance(cell, _Element) and cell.tag in ("td", "th")
            ]
            for row in _find(table, "tr", stop="table")
        ]


def _iter_elements(root: _Element) -> Iterator[_Element]:
    """Элементы в порядке документа (без рекурсии)."""
    stack = [root]
    while stack:
        element = stack.pop()
        yield element
        stack.extend(c for c in reversed(element.children) if isinstance(c, _Element))


def _find(element: _Element, tag: str, *, stop: str) -> Iterator[_Element]:
    """Потомки с тегом `tag`, не заходя внутрь `stop` (вложенные таблицы)."""
    for child in element.children:
        if isinstance(child, _Element):
            if child.tag == tag:
                yield child
            elif child.tag != stop:
                yield from _find(child, tag, stop=stop)


def _raw_text(element: _Element) -> str:
    return "".join(c if isinstance(c, str) else _raw_text(c) for c in element.children)


def _collapse(text: str) -> str:
    return " ".join(text.split())


def _content_root(elements: list[_Element], document: _Element) -> _Element:
    mains = [e for e in elements if e.tag == "main" or e.attrs.get("role", "").lower() == "main"]
    articles = [e for e in elements if e.tag == "article"]
    candidates = mains + (articles if len(articles) == 1 else [])
    for candidate in candidates:
        if _raw_text(candidate).strip():
            return candidate
    return next((e for e in elements if e.tag == "body"), document)


def _is_layout_table(table: _Element) -> bool:
    """Таблица-вёрстка (старые сайты): внутри вложенные таблицы, заголовки, секции."""
    if table.attrs.get("role", "").lower() in ("presentation", "none"):
        return True
    return any(e.tag in _LAYOUT_MARKERS for e in _iter_elements(table) if e is not table)


def _fence(text: str) -> str:
    text = text.replace("\r\n", "\n").strip("\n").rstrip()
    if not text.strip():
        return ""
    fence = "~~~" if "```" in text else "```"
    return f"{fence}\n{text}\n{fence}"


def _links(elements: list[_Element], base: str) -> list[str]:
    links: dict[str, None] = {}
    for element in elements:
        href = element.attrs.get("href")
        if element.tag not in ("a", "area") or href is None:
            continue
        if "nofollow" in element.attrs.get("rel", "").lower().split():
            continue
        url = normalize_url(href, base)
        if url is not None:
            links.setdefault(url)
    return list(links)
