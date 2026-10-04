"""robots.txt для краулера `website` (RFC 9309, ADR-0013).

Stdlib `urllib.robotparser` не понимает `*` и `$` в путях, поэтому разбор свой:
группа — подряд идущие `User-agent` и их правила; берутся группы с нашим токеном агента
(без учёта регистра), а если их нет — группы `*`; правила нескольких групп объединяются. Решает
самое длинное совпадающее правило, при равной длине — `Allow`. Учитываются `Crawl-delay`
(вне RFC, но его ставят сайты) и `Sitemap`.

Загрузка: 2xx — правила; 4xx — ограничений нет; 5xx и сетевые сбои — `RobotsUnavailableError`
(синхронизация падает, а не обходит сайт без правил и не удаляет его документы).
"""

import re
from dataclasses import dataclass, field
from urllib.parse import quote, unquote, urlsplit

from app.modules.knowledge.infrastructure.web_client import WebClient, WebFetchError
from app.modules.knowledge.infrastructure.web_urls import normalize_url

# RFC 9309: разбирать нужно не меньше 500 КиБ; остальное отбрасывается.
MAX_ROBOTS_BYTES = 512 * 1024

_LINE = re.compile(r"^\s*([A-Za-z-]+)\s*:\s*(.*?)\s*$")


class RobotsUnavailableError(Exception):
    """robots.txt недоступен (5xx, сеть): обходить сайт нельзя."""


@dataclass(frozen=True, slots=True)
class _Rule:
    allow: bool
    pattern: re.Pattern[str]
    length: int


@dataclass(frozen=True, slots=True)
class RobotsRules:
    rules: tuple[_Rule, ...] = ()
    crawl_delay: float | None = None
    sitemaps: tuple[str, ...] = field(default=())

    def allowed(self, url: str) -> bool:
        parts = urlsplit(url)
        path = _canonical_path(parts.path or "/") + (f"?{parts.query}" if parts.query else "")
        if path == "/robots.txt":
            return True
        best: _Rule | None = None
        for rule in self.rules:
            if rule.pattern.match(path) and (
                best is None or (rule.length, rule.allow) > (best.length, best.allow)
            ):
                best = rule
        return best is None or best.allow


ALLOW_ALL = RobotsRules()


def parse_robots(text: str, user_agent: str, base_url: str) -> RobotsRules:
    """`user_agent` — токен продукта (`TesseraBot`), без версии; регистр не важен."""
    agent = user_agent.split("/", 1)[0].strip().lower()
    groups: list[tuple[list[str], list[tuple[str, str]]]] = []
    sitemaps: list[str] = []
    in_agents = False
    for raw in text.splitlines():
        match = _LINE.match(raw.split("#", 1)[0])
        if not match:
            continue
        key, value = match.group(1).lower(), match.group(2)
        if key == "sitemap":
            url = normalize_url(value, base_url)
            if url is not None:
                sitemaps.append(url)
        elif key == "user-agent":
            if not in_agents:
                groups.append(([], []))
                in_agents = True
            groups[-1][0].append(value.lower())
        elif key in ("allow", "disallow", "crawl-delay") and groups:
            in_agents = False
            groups[-1][1].append((key, value))

    matched = _matching_lines(groups, agent)
    rules: list[_Rule] = []
    delay: float | None = None
    for key, value in matched:
        if key == "crawl-delay":
            try:
                delay = max(delay or 0.0, float(value))
            except ValueError:
                continue
        elif value:  # пустой Disallow ничего не запрещает
            pattern = _canonical_path(value)
            rules.append(_Rule(key == "allow", _compile(pattern), len(pattern)))
    return RobotsRules(tuple(rules), delay, tuple(dict.fromkeys(sitemaps)))


async def load_robots(client: WebClient, origin: str, user_agent: str) -> RobotsRules:
    """robots.txt сайта `origin` (`https://example.ru`)."""
    url = f"{origin}/robots.txt"
    try:
        response = await client.get(url, max_bytes=MAX_ROBOTS_BYTES, truncate=True)
    except WebFetchError as e:
        raise RobotsUnavailableError(str(e)) from e
    if 400 <= response.status < 500:
        return ALLOW_ALL
    if not 200 <= response.status < 300:
        raise RobotsUnavailableError(f"{url}: HTTP {response.status}")
    text = response.body.decode("utf-8", errors="replace").lstrip("﻿")
    return parse_robots(text, user_agent, response.url)


def _matching_lines(
    groups: list[tuple[list[str], list[tuple[str, str]]]], agent: str
) -> list[tuple[str, str]]:
    """Правила групп нашего агента, а если таких нет — групп `*`."""
    for token in (agent, "*"):
        lines = [line for agents, group in groups if token in agents for line in group]
        if lines or any(token in agents for agents, _ in groups):
            return lines
    return []


def _canonical_path(path: str) -> str:
    """Процент-кодирование в одном виде, чтобы `/%D0%B0` и `/а` совпадали."""
    return quote(unquote(path), safe="/?=&*$%:@!,;+~-._")


def _compile(pattern: str) -> re.Pattern[str]:
    anchored = pattern.endswith("$")
    body = pattern[:-1] if anchored else pattern
    regex = ".*".join(re.escape(part) for part in body.split("*"))
    return re.compile(regex + ("$" if anchored else ""))
