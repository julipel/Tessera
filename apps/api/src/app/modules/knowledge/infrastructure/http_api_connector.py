"""Коннектор `http_api`: JSON API клиента → Entity или Document по маппингу из конфига.

Только GET (read-only) через `WebClient` — та же SSRF-защита и лимиты, что у краулера
(ADR-0013); редиректы и `next_url` — только на хост из `url`, чтобы заголовок авторизации
не ушёл на чужой сервер. Секреты — ссылки на env `SOURCE_SECRET_*` (`source_secrets`, ADR-0017).

Записи — массив по пути `records` (`data.items`; пусто — корень ответа), страницы — по
`pagination`. Поля маппинга — пути в записи (`price.value`, `images.0.url`).

Полный discover удаляет всё, чего нет в листинге, поэтому неполный листинг — ошибка
синхронизации: неверный конфиг или секрет, не-2xx или не-JSON ответ любой страницы, нет
массива по пути `records`, больше `max_pages` страниц или `max_records` записей, зацикленный
курсор. Пустой или повторяющийся id, пустое название, неразбираемая цена — ошибки элементов.
Курсора синхронизации нет: каждая — полная, записи discover кэшируются для `fetch`.
"""

import base64
import json
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any, Literal, Self, cast
from urllib.parse import urlsplit
from uuid import UUID

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from app.modules.knowledge.domain.entities import SourceKind
from app.modules.knowledge.domain.ingestion import (
    DocumentItem,
    Listing,
    RawItem,
    RawItemRef,
    SourceSpec,
)
from app.modules.knowledge.infrastructure.entity_mapping import (
    AttributeSpec,
    AttributeType,
    EntityFields,
    Value,
    map_entity,
    text,
)
from app.modules.knowledge.infrastructure.source_secrets import SourceSecretError, SourceSecrets
from app.modules.knowledge.infrastructure.web_client import WebClient, WebFetchError
from app.modules.knowledge.infrastructure.web_urls import normalize_url
from app.modules.shared.kernel import TenantId

MAX_RESPONSE_BYTES = 20 * 1024 * 1024
MAX_PAGES_CAP = 1000
MAX_RECORDS_CAP = 100_000
DEFAULT_CACHE_SIZE = 4

# Заголовки, которые задаёт клиент или которые несут секреты (секреты — только через `auth`).
_RESERVED_HEADERS = frozenset(
    {"authorization", "proxy-authorization", "cookie", "host", "user-agent", "accept-encoding"}
)


class HttpApiSourceError(Exception):
    """Ошибка источника целиком (конфиг, секрет, ответ API) или его записи."""


class _Config(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class HttpAuth(_Config):
    """`bearer` — `Authorization: Bearer <секрет>`; `header` — секрет в заголовке `header`;
    `basic` — секрет вида `user:password`."""

    type: Literal["bearer", "header", "basic"]
    secret_env: str
    header: str | None = None

    @model_validator(mode="after")
    def _check_header(self) -> Self:
        if (self.type == "header") != (self.header is not None):
            raise ValueError("header задаётся только и обязательно для type=header")
        if self.header is not None and self.header.lower() in _RESERVED_HEADERS:
            raise ValueError(f"заголовок {self.header!r} нельзя использовать для секрета")
        return self


class HttpPagination(_Config):
    """`page` — номер страницы с `start`; `offset` — смещение по `page_size`; `cursor` —
    значение по `cursor_path` ответа в параметр `param`; `next_url` — адрес следующей
    страницы по `next_url_path`. Конец: пустая/неполная страница, нет курсора или адреса."""

    type: Literal["none", "page", "offset", "cursor", "next_url"] = "none"
    param: str | None = None  # по умолчанию page / offset / cursor
    start: int = Field(default=1, ge=0)
    size_param: str | None = None
    page_size: int | None = Field(default=None, ge=1, le=10_000)
    cursor_path: str | None = None
    next_url_path: str | None = None
    max_pages: int = Field(default=100, ge=1, le=MAX_PAGES_CAP)

    @model_validator(mode="after")
    def _check_type(self) -> Self:
        if (self.size_param is None) != (self.page_size is None):
            raise ValueError("size_param и page_size задаются вместе")
        if self.type == "offset" and self.page_size is None:
            raise ValueError("для offset нужны size_param и page_size")
        if self.type == "cursor" and self.cursor_path is None:
            raise ValueError("для cursor нужен cursor_path")
        if self.type == "next_url" and self.next_url_path is None:
            raise ValueError("для next_url нужен next_url_path")
        return self

    @property
    def page_param(self) -> str:
        return self.param or {"page": "page", "offset": "offset"}.get(self.type, "cursor")


class HttpAttribute(_Config):
    path: str
    type: AttributeType = "string"


class HttpEntityMapping(_Config):
    entity_type: str = Field(min_length=1, max_length=64)
    fields: EntityFields
    attributes: dict[str, str | HttpAttribute] = Field(default_factory=dict)
    currency: str | None = None  # если поля валюты нет или оно пустое

    def attribute_specs(self) -> dict[str, AttributeSpec]:
        return {
            name: AttributeSpec(spec)
            if isinstance(spec, str)
            else AttributeSpec(spec.path, spec.type)
            for name, spec in self.attributes.items()
        }


class HttpDocumentMapping(_Config):
    external_id: str
    title: str
    text: str
    url: str | None = None


class HttpApiSourceConfig(_Config):
    """`Source.config` источника `http_api` (architecture.md §8)."""

    url: str
    params: dict[str, str | int | float | bool] = Field(default_factory=dict)
    headers: dict[str, str] = Field(default_factory=dict)
    auth: HttpAuth | None = None
    records: str = ""
    pagination: HttpPagination = HttpPagination()
    entity: HttpEntityMapping | None = None
    document: HttpDocumentMapping | None = None
    max_records: int = Field(default=10_000, ge=1, le=MAX_RECORDS_CAP)

    @model_validator(mode="after")
    def _check(self) -> Self:
        if (self.entity is None) == (self.document is None):
            raise ValueError("нужен ровно один маппинг: entity или document")
        reserved = [h for h in self.headers if h.lower() in _RESERVED_HEADERS]
        if reserved:
            raise ValueError(f"заголовки {reserved} нельзя задавать (секреты — через auth)")
        return self

    @property
    def id_path(self) -> str:
        if self.entity is not None:
            return self.entity.fields.external_id
        assert self.document is not None
        return self.document.external_id


@dataclass(frozen=True, slots=True)
class _Record:
    """Элемент записи или ошибка, которую `fetch` отдаст как ошибку элемента."""

    item: RawItem | None
    error: str | None = None


class HttpApiConnector:
    kind = SourceKind.HTTP_API

    def __init__(
        self,
        client: WebClient,
        secrets: SourceSecrets,
        *,
        cache_size: int = DEFAULT_CACHE_SIZE,
    ) -> None:
        self.client = client
        self.secrets = secrets
        self.cache_size = cache_size
        # Записи последнего discover источника: fetch берёт их, не запрашивая API заново.
        self._cache: OrderedDict[tuple[TenantId, UUID], dict[str, _Record]] = OrderedDict()

    async def discover(self, source: SourceSpec) -> Listing:
        records = await self._load(source)
        key = (source.tenant_id, source.source_id)
        self._cache[key] = records
        self._cache.move_to_end(key)
        while len(self._cache) > self.cache_size:
            self._cache.popitem(last=False)
        return Listing([RawItemRef(external_id) for external_id in records])

    async def changed_since(self, source: SourceSpec, cursor: str) -> Listing:
        return await self.discover(source)

    async def fetch(self, source: SourceSpec, ref: RawItemRef) -> RawItem:
        records = self._cache.get((source.tenant_id, source.source_id))
        if records is None or ref.external_id not in records:
            records = await self._load(source)
        record = records.get(ref.external_id)
        if record is None:
            raise HttpApiSourceError(f"запись не найдена: {ref.external_id}")
        if record.item is None:
            raise HttpApiSourceError(record.error)
        return record.item

    async def _load(self, source: SourceSpec) -> dict[str, _Record]:
        config = _parse_config(source.config)
        start = normalize_url(config.url)
        if start is None:
            raise HttpApiSourceError(f"url: не http(s)-URL: {config.url!r}")
        host = urlsplit(start).hostname or ""
        headers = {**config.headers, **self._auth_headers(config.auth)}
        pages = _Pages(config.pagination, host)
        url: str | None = pages.first(_with_params(start, config.params))
        records: list[Any] = []
        for page in range(config.pagination.max_pages):
            assert url is not None
            response_url, body = await self._get_json(url, headers, host)
            batch = _lookup(body, config.records)
            if not isinstance(batch, list):
                where = f"по пути «{config.records}»" if config.records else "в корне ответа"
                raise HttpApiSourceError(f"{response_url}: нет массива записей {where}")
            records += batch
            if len(records) > config.max_records:
                raise HttpApiSourceError(f"больше max_records={config.max_records} записей")
            url = pages.next(url, response_url, body, len(batch), page)
            if url is None:
                break
        else:
            raise HttpApiSourceError(
                f"больше max_pages={config.pagination.max_pages} страниц: листинг неполный"
            )
        return _map_records(config, records)

    def _auth_headers(self, auth: HttpAuth | None) -> dict[str, str]:
        if auth is None:
            return {}
        try:
            secret = self.secrets.get(auth.secret_env)
        except SourceSecretError as e:
            raise HttpApiSourceError(str(e)) from e
        if auth.type == "bearer":
            return {"Authorization": f"Bearer {secret}"}
        if auth.type == "basic":
            return {"Authorization": f"Basic {base64.b64encode(secret.encode()).decode()}"}
        assert auth.header is not None
        return {auth.header: secret}

    async def _get_json(self, url: str, headers: dict[str, str], host: str) -> tuple[str, Any]:
        try:
            response = await self.client.get(
                url, max_bytes=MAX_RESPONSE_BYTES, allowed_hosts={host}, headers=headers
            )
        except WebFetchError as e:
            raise HttpApiSourceError(str(e)) from e
        if not 200 <= response.status < 300:
            raise HttpApiSourceError(f"{response.url}: HTTP {response.status}")
        try:
            return response.url, json.loads(response.body)
        except ValueError as e:
            raise HttpApiSourceError(f"{response.url}: ответ не JSON: {e}") from e


class _Pages:
    """Адреса страниц по `pagination`; следит за зацикливанием курсора и адреса."""

    def __init__(self, config: HttpPagination, host: str) -> None:
        self.config = config
        self.host = host
        self.seen: set[str] = set()

    def first(self, url: str) -> str:
        config = self.config
        if config.size_param is not None:
            url = _with_params(url, {config.size_param: config.page_size})
        if config.type == "page":
            url = _with_params(url, {config.page_param: config.start})
        elif config.type == "offset":
            url = _with_params(url, {config.page_param: 0})
        return url

    def next(self, url: str, response_url: str, body: Any, count: int, page: int) -> str | None:
        config = self.config
        full = config.page_size is None or count >= config.page_size
        if config.type == "page":
            if count == 0 or not full:
                return None
            return _with_params(url, {config.page_param: config.start + page + 1})
        if config.type == "offset":
            assert config.page_size is not None
            return (
                _with_params(url, {config.page_param: (page + 1) * config.page_size})
                if full
                else None
            )
        if config.type == "cursor":
            assert config.cursor_path is not None
            cursor = _scalar_text(body, config.cursor_path)
            if cursor is None:
                return None
            self._check_new(cursor, "курсор")
            return _with_params(url, {config.page_param: cursor})
        if config.type == "next_url":
            assert config.next_url_path is not None
            link = _scalar_text(body, config.next_url_path)
            if link is None:
                return None
            target = normalize_url(link, response_url)
            if target is None or urlsplit(target).hostname != self.host:
                raise HttpApiSourceError(f"next_url ведёт за пределы API: {link!r}")
            self._check_new(target, "next_url")
            return target
        return None

    def _check_new(self, value: str, what: str) -> None:
        if value in self.seen:
            raise HttpApiSourceError(f"{what} повторился ({value!r}): пагинация зациклена")
        self.seen.add(value)


def _parse_config(config: dict[str, Any]) -> HttpApiSourceConfig:
    try:
        return HttpApiSourceConfig.model_validate(config)
    except ValidationError as e:
        raise HttpApiSourceError(f"неверный конфиг источника http_api: {e}") from e


def _with_params(url: str, params: dict[str, Any]) -> str:
    values = {k: str(v).lower() if isinstance(v, bool) else str(v) for k, v in params.items()}
    return str(httpx.URL(url).copy_merge_params(values))


_MISSING = object()


def _lookup(data: Any, path: str) -> Any:
    """Значение по пути `a.b.0.c`; пустой путь — сам объект; нет пути — `_MISSING`."""
    current = data
    for part in path.split(".") if path else []:
        if isinstance(current, dict) and part in current:
            current = current[part]
        elif isinstance(current, list) and part.isdigit() and int(part) < len(current):
            current = current[int(part)]
        else:
            return _MISSING
    return current


def _scalar(data: Any, path: str) -> Value:
    value = _lookup(data, path)
    if value is _MISSING:
        return None
    if isinstance(value, dict | list):
        raise ValueError(f"«{path}»: не скалярное значение")
    return cast(Value, value)


def _scalar_text(data: Any, path: str) -> str | None:
    try:
        return text(_scalar(data, path))
    except ValueError as e:
        raise HttpApiSourceError(str(e)) from e


def _map_records(config: HttpApiSourceConfig, records: list[Any]) -> dict[str, _Record]:
    id_path = config.id_path
    result: dict[str, _Record] = {}
    first: dict[str, int] = {}
    for number, record in enumerate(records, start=1):
        where = f"запись {number}"
        try:
            if not isinstance(record, dict):
                raise ValueError("не объект")
            external_id = text(_scalar(record, id_path))
            if external_id is None:
                raise ValueError(f"пустой id («{id_path}»)")
        except ValueError as e:
            result[f"#{number}"] = _Record(None, f"{where}: {e}")
            continue
        try:
            item = _map_record(config, record, external_id)
            entry = _Record(item)
        except ValueError as e:
            entry = _Record(None, f"{where}: {e}")
        if external_id in result:
            # Какая из записей с одним id верная, неизвестно: ошибка элемента для обеих.
            error = f"id «{external_id}» повторяется: записи {first[external_id]}, {number}"
            result[external_id] = _Record(None, error)
            continue
        result[external_id] = entry
        first[external_id] = number
    return result


def _map_record(config: HttpApiSourceConfig, record: dict[str, Any], external_id: str) -> RawItem:
    if config.entity is not None:
        return map_entity(
            lambda path: _scalar(record, path),
            entity_type=config.entity.entity_type,
            external_id=external_id,
            fields=config.entity.fields,
            attributes=config.entity.attribute_specs(),
            currency=config.entity.currency,
        )
    mapping = config.document
    assert mapping is not None
    title = text(_scalar(record, mapping.title))
    if title is None:
        raise ValueError(f"пустое название («{mapping.title}»)")
    body = text(_scalar(record, mapping.text))
    if body is None:
        raise ValueError(f"пустой текст («{mapping.text}»)")
    url = text(_scalar(record, mapping.url)) if mapping.url else None
    return DocumentItem(external_id=external_id, title=title, text=body, url=url)
