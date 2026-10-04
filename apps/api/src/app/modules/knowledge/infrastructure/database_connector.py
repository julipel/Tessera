"""Коннектор `database`: SQL-запрос из конфига к PostgreSQL клиента → Entity или Document.

Read-only и изоляция от сети платформы (ADR-0018):
- DSN — секрет `dsn_env` (ADR-0017). Он разбирается здесь, а asyncpg получает только host,
  port, user, password и database: иначе asyncpg подставил бы PGHOST/PGUSER и `~/.pgpass`
  платформы, а параметры DSN стали бы настройками сессии.
- Имя хоста резолвится здесь; все адреса должны быть публичными (`is_public_address`,
  ADR-0013) или входить в allowlist платформы `SOURCE_DB_ALLOWED_NETWORKS`. Подключение —
  к проверенному IP (DNS rebinding не сработает), поэтому TLS без проверки имени хоста:
  `disable | require | verify-ca`.
- Транзакция READ ONLY и `default_transaction_read_only`; запрос — подготовленный оператор
  (несколько команд через `;` он не примет); `statement_timeout`, общий таймаут.

Полный discover удаляет всё, чего нет в листинге, поэтому неполный результат — ошибка
синхронизации: неверный конфиг, DSN или адрес, ошибка запроса или подключения, таймаут,
больше `max_rows` строк, нет колонки из маппинга. Ошибки строк (пустой или повторяющийся id,
нескалярное значение) — ошибки элементов. Курсора нет: каждая синхронизация полная, записи
discover кэшируются для `fetch`.
"""

import asyncio
import ipaddress
import ssl
from collections import OrderedDict
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, time
from decimal import Decimal
from typing import Any, Literal, Self
from urllib.parse import unquote, urlsplit
from uuid import UUID

import asyncpg
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from app.modules.knowledge.domain.entities import SourceKind
from app.modules.knowledge.domain.ingestion import Listing, RawItem, RawItemRef, SourceSpec
from app.modules.knowledge.infrastructure.entity_mapping import (
    AttributeColumn,
    AttributeSpec,
    EntityFields,
    Scalar,
    Value,
)
from app.modules.knowledge.infrastructure.record_mapping import (
    DocumentFields,
    EntityMapping,
    ItemMapping,
    MappedRecord,
    Record,
    map_records,
)
from app.modules.knowledge.infrastructure.source_secrets import SourceSecretError, SourceSecrets
from app.modules.knowledge.infrastructure.web_guard import (
    Resolver,
    is_public_address,
    resolve_host,
)
from app.modules.shared.kernel import TenantId

type Network = ipaddress.IPv4Network | ipaddress.IPv6Network

MAX_ROWS_CAP = 200_000
MAX_TIMEOUT_S = 600.0
DEFAULT_CONNECT_TIMEOUT_S = 10.0
DEFAULT_CACHE_SIZE = 4
FETCH_BATCH = 1000


class DatabaseSourceError(Exception):
    """Ошибка источника целиком (конфиг, DSN, адрес, запрос) или его строки."""


class _Config(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class DatabaseEntityMapping(_Config):
    entity_type: str = Field(min_length=1, max_length=64)
    fields: EntityFields
    attributes: dict[str, str | AttributeColumn] = Field(default_factory=dict)
    currency: str | None = None  # если колонки валюты нет или значение пустое

    def to_mapping(self) -> EntityMapping:
        attributes = {
            name: AttributeSpec(spec) if isinstance(spec, str) else spec.spec()
            for name, spec in self.attributes.items()
        }
        return EntityMapping(self.entity_type, self.fields, attributes, self.currency)


class DatabaseSourceConfig(_Config):
    """`Source.config` источника `database` (architecture.md §8)."""

    dsn_env: str
    query: str = Field(min_length=1, max_length=20_000)
    sslmode: Literal["disable", "require", "verify-ca"] = "require"
    timeout_s: float = Field(default=60.0, gt=0, le=MAX_TIMEOUT_S)
    max_rows: int = Field(default=50_000, ge=1, le=MAX_ROWS_CAP)
    entity: DatabaseEntityMapping | None = None
    document: DocumentFields | None = None

    @model_validator(mode="after")
    def _check(self) -> Self:
        if (self.entity is None) == (self.document is None):
            raise ValueError("нужен ровно один маппинг: entity или document")
        return self

    def mapping(self) -> ItemMapping:
        if self.entity is not None:
            return self.entity.to_mapping()
        assert self.document is not None
        return self.document


@dataclass(frozen=True, slots=True)
class Dsn:
    host: str
    port: int
    user: str
    password: str
    database: str


def parse_dsn(dsn: str) -> Dsn:
    """Разбор `postgresql://user:password@host:port/database` без параметров.

    Текст DSN содержит пароль и в тексты ошибок не попадает.
    """
    try:
        parts = urlsplit(dsn.strip())
        port = parts.port or 5432
    except ValueError:
        raise DatabaseSourceError("DSN: не разбирается как postgresql://…") from None
    if parts.scheme not in ("postgres", "postgresql"):
        raise DatabaseSourceError("DSN: схема должна быть postgresql://")
    if parts.query or parts.fragment:
        raise DatabaseSourceError("DSN: параметры не поддерживаются (sslmode — в конфиге)")
    if "," in parts.netloc:
        raise DatabaseSourceError("DSN: допускается один хост")
    database = unquote(parts.path.removeprefix("/"))
    result = Dsn(
        host=parts.hostname or "",
        port=port,
        user=unquote(parts.username or ""),
        password=unquote(parts.password or ""),
        database=database,
    )
    missing = [
        name for name in ("host", "user", "password", "database") if not getattr(result, name)
    ]
    if missing or "/" in database:
        raise DatabaseSourceError(f"DSN: нужны хост, пользователь, пароль и база (нет: {missing})")
    return result


def parse_networks(values: Collection[str]) -> list[Network]:
    """CIDR из `SOURCE_DB_ALLOWED_NETWORKS`; неверное значение — `ValueError`."""
    return [ipaddress.ip_network(value, strict=False) for value in values]


class DatabaseConnector:
    kind = SourceKind.DATABASE

    def __init__(
        self,
        secrets: SourceSecrets,
        *,
        allowed_networks: Sequence[Network] = (),
        resolver: Resolver = resolve_host,
        connect_timeout_s: float = DEFAULT_CONNECT_TIMEOUT_S,
        cache_size: int = DEFAULT_CACHE_SIZE,
    ) -> None:
        self.secrets = secrets
        self.allowed_networks = list(allowed_networks)
        self.resolver = resolver
        self.connect_timeout_s = connect_timeout_s
        self.cache_size = cache_size
        # Записи последнего discover источника: fetch берёт их, не выполняя запрос заново.
        self._cache: OrderedDict[tuple[TenantId, UUID], dict[str, MappedRecord]] = OrderedDict()

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
            raise DatabaseSourceError(f"запись не найдена: {ref.external_id}")
        if record.item is None:
            raise DatabaseSourceError(record.error)
        return record.item

    async def _load(self, source: SourceSpec) -> dict[str, MappedRecord]:
        config = _parse_config(source.config)
        try:
            dsn = parse_dsn(self.secrets.get(config.dsn_env))
        except SourceSecretError as e:
            raise DatabaseSourceError(str(e)) from e
        address = await self._checked_address(dsn.host, dsn.port)
        mapping = config.mapping()
        try:
            async with asyncio.timeout(self.connect_timeout_s + config.timeout_s):
                rows = await self._query(config, dsn, address, _columns(mapping))
        except TimeoutError as e:
            raise DatabaseSourceError(
                f"{dsn.host}: нет ответа (подключение {self.connect_timeout_s} с, "
                f"запрос {config.timeout_s} с)"
            ) from e
        except (asyncpg.PostgresError, asyncpg.InterfaceError, OSError) as e:
            raise DatabaseSourceError(f"{dsn.host}: {type(e).__name__}: {e}") from e
        return map_records((_row_record(row) for row in rows), mapping)

    async def _checked_address(self, host: str, port: int) -> str:
        try:
            addresses = await self.resolver(host, port)
        except OSError as e:
            raise DatabaseSourceError(f"не удалось разрешить {host}: {e}") from e
        blocked = [a for a in addresses if not self._allowed(a)]
        if blocked or not addresses:
            raise DatabaseSourceError(
                f"{host}: адреса {blocked or addresses} не публичные и не входят "
                "в SOURCE_DB_ALLOWED_NETWORKS"
            )
        return addresses[0]

    def _allowed(self, address: str) -> bool:
        if is_public_address(address):
            return True
        try:
            ip = ipaddress.ip_address(address)
        except ValueError:
            return False
        if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
            ip = ip.ipv4_mapped
        return any(ip in network for network in self.allowed_networks)

    async def _query(
        self, config: DatabaseSourceConfig, dsn: Dsn, address: str, columns: list[str]
    ) -> list[Mapping[str, Any]]:
        timeout_ms = str(int(config.timeout_s * 1000))
        connection = await asyncpg.connect(
            host=address,
            port=dsn.port,
            user=dsn.user,
            password=dsn.password,
            database=dsn.database,
            ssl=_ssl(config.sslmode),
            timeout=self.connect_timeout_s,
            statement_cache_size=0,
            server_settings={
                "application_name": "tessera-ingestion",
                "default_transaction_read_only": "on",
                "statement_timeout": timeout_ms,
                "idle_in_transaction_session_timeout": timeout_ms,
            },
        )
        try:
            async with connection.transaction(readonly=True):
                statement = await connection.prepare(config.query)
                names = {attribute.name for attribute in statement.get_attributes()}
                missing = [c for c in dict.fromkeys(columns) if c not in names]
                if missing:
                    listed = ", ".join(f"«{c}»" for c in missing)
                    raise DatabaseSourceError(f"в результате запроса нет колонок {listed}")
                cursor = await statement.cursor()
                rows: list[Mapping[str, Any]] = []
                while batch := await cursor.fetch(
                    min(FETCH_BATCH, config.max_rows + 1 - len(rows))
                ):
                    rows += batch
                    if len(rows) > config.max_rows:
                        raise DatabaseSourceError(
                            f"больше max_rows={config.max_rows} строк: листинг неполный"
                        )
                return rows
        finally:
            await connection.close(timeout=self.connect_timeout_s)


def _parse_config(config: dict[str, Any]) -> DatabaseSourceConfig:
    try:
        return DatabaseSourceConfig.model_validate(config)
    except ValidationError as e:
        raise DatabaseSourceError(f"неверный конфиг источника database: {e}") from e


def _ssl(mode: str) -> ssl.SSLContext | str:
    if mode != "verify-ca":
        return mode
    # Подключение — по IP, имя хоста в сертификате не сверить: только цепочка до системных CA.
    context = ssl.create_default_context()
    context.check_hostname = False
    return context


def _columns(mapping: ItemMapping) -> list[str]:
    if isinstance(mapping, EntityMapping):
        fields = [c for c in mapping.fields.model_dump().values() if c is not None]
        return [*fields, *(a.key for a in mapping.attributes.values())]
    return [c for c in (mapping.external_id, mapping.title, mapping.text, mapping.url) if c]


def _row_record(row: Mapping[str, Any]) -> Record:
    def value(column: str) -> Value:
        raw = row[column]
        if raw is None:
            return None
        if isinstance(raw, list) and all(v is not None for v in raw):
            # Массив Postgres (text[], int[]) — для атрибутов `list`; поля его отклонят.
            return [_scalar(column, v) for v in raw]
        return _scalar(column, raw)

    return value


def _scalar(column: str, raw: Any) -> Scalar:
    if isinstance(raw, str | int | float | Decimal | datetime | date | time):
        return raw  # bool — подкласс int
    if isinstance(raw, UUID):
        return str(raw)
    raise ValueError(f"«{column}»: не скалярное значение ({type(raw).__name__})")
