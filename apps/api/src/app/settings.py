"""Настройки приложения из переменных окружения (и `.env` в корне репозитория)."""

import ipaddress
import os
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Literal, Self
from urllib.parse import urlsplit

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, DotEnvSettingsSource, SettingsConfigDict

# src/app/settings.py → корень репозитория на 4 уровня выше пакета.
_REPO_ROOT = Path(__file__).resolve().parents[4]

ENV_FILES: tuple[Path | str, ...] = (_REPO_ROOT / ".env", ".env")
SOURCE_SECRET_PREFIX = "SOURCE_SECRET_"  # noqa: S105 — префикс имени env, не секрет

# Пароль БД из .env.example и значений по умолчанию.
_DEV_DB_PASSWORDS = frozenset({"app"})

AppEnv = Literal["local", "test", "staging", "production"]
LogLevel = Literal["DEBUG", "INFO", "WARNING", "ERROR"]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=ENV_FILES,
        env_file_encoding="utf-8",
        extra="ignore",
        # Ошибка настроек не выводит значения из env (ключи провайдеров, адрес БД) в лог.
        hide_input_in_errors=True,
    )

    app_env: AppEnv = "local"
    log_level: LogLevel = "INFO"
    # Адреса с паролями — без repr: не попадают в вывод настроек и трассировки (S-17).
    database_url: str = Field(default="postgresql+asyncpg://app:app@localhost:5432/app", repr=False)
    database_echo: bool = False
    # Пул соединений API на процесс. SSE-ход держит соединение до конца ответа модели
    # (P7-05, кандидат P7-08), поэтому одновременных ходов на процесс не больше
    # DB_POOL_SIZE + DB_MAX_OVERFLOW; остальные ждут свободное соединение до таймаута пула.
    db_pool_size: int = 5
    db_max_overflow: int = 10
    # Ожидание свободного соединения пула и потолок одного запроса к БД, с (REVIEW.md: S-4).
    db_pool_timeout_s: float = Field(default=30, gt=0)
    db_statement_timeout_s: float = Field(default=30, gt=0)
    redis_url: str = Field(default="redis://localhost:6379/0", repr=False)
    qdrant_url: str = "http://localhost:6333"
    qdrant_api_key: SecretStr | None = None
    # Коллекция чанков знаний, одна на окружение (architecture.md §8).
    qdrant_collection: str = "knowledge"
    # Dense-эмбеддинги чанков: OpenAI Embeddings API (ключ и base_url — OPENAI_*).
    # Смена модели или размерности требует новой коллекции и переиндексации.
    embedding_model: str = "text-embedding-3-small"
    embedding_dimensions: int = 1536
    embedding_batch_size: int = 128
    # Реранкинг search_knowledge: HTTP `/rerank` в формате Cohere/Jina (ADR-0015), полный URL
    # endpoint. Без URL и модели реранкинг выключен (`rerank: true` в конфиге не действует).
    rerank_url: str | None = None
    rerank_api_key: SecretStr | None = None
    rerank_model: str | None = None
    # Короткий: весь search_knowledge (эмбеддинг, Qdrant, реранкинг) укладывается в 10 с.
    rerank_timeout_s: float = 3.0
    # Файлы источников `file`: <dir>/<tenant_id>/<source_id>/ (ADR-0012).
    knowledge_files_dir: Path = _REPO_ROOT / "data" / "knowledge"
    # Краулер источников `website` (ADR-0013).
    crawler_user_agent: str = "TesseraBot/0.1"
    crawler_timeout_s: float = 15.0
    # Дедлайн одной синхронизации источника в воркере: обход сайта (`max_duration_s` ≤ 6 ч)
    # плюс запас на эмбеддинги и запись. По нему синхронизация завершается failed;
    # таймаут задачи arq — с запасом сверху (worker.py).
    sync_timeout_s: float = 7 * 3600
    # Приватные подсети, к которым можно подключать источники `database` (ADR-0018), JSON-список
    # CIDR. Публичные адреса разрешены всегда; локально — ["127.0.0.0/8"].
    source_db_allowed_networks: list[str] = []
    # Ключи виджета, которые `make seed` выдаёт новым тенантам: slug=key[,slug=key…] (или key,
    # если тенант один); у тенанта без ключа здесь — сгенерировать.
    seed_widget_key: str | None = None
    # Origin своего веб-чата (env — JSON-список): CORS и проверка Origin по ключу виджета —
    # с него ключ работает всегда, сайты тенанта задаёт allowed_origins (ADR-0022). Он же —
    # origin админки: изменяющие запросы по cookie сессии принимаются только с него (ADR-0044).
    # Сравнение точное: «*», путь и слэш в конце отклоняются при старте.
    cors_origins: list[str] = ["http://localhost:3000"]
    # LLM-провайдеры (ADR-0010); base_url пусто — официальный endpoint.
    openai_api_key: SecretStr | None = None
    openai_base_url: str | None = None
    # Chat Completions OpenAI-совместимых API (прокси, локальные серверы).
    openai_compatible_api_key: SecretStr | None = None
    openai_compatible_base_url: str | None = None
    anthropic_api_key: SecretStr | None = None
    anthropic_base_url: str | None = None
    # Лимиты частоты публичного API (P7-04a, ADR-0030), фиксированное окно в Redis; 0 — правило
    # выключено. IP — `request.client.host` (за прокси — uvicorn --proxy-headers).
    rate_turns_per_ip_per_min: int = 20
    rate_turns_per_tenant_per_min: int = 300
    rate_conversations_per_ip_per_hour: int = 30
    # Трейсы LLM в Langfuse (P7-03, ADR-0029); без обоих ключей трейсинг выключен.
    # LANGFUSE_HOST пусто — Langfuse Cloud (EU).
    langfuse_public_key: str | None = None
    langfuse_secret_key: SecretStr | None = None
    langfuse_host: str | None = None
    # Таймаут отправки пачки трейсов (фоновый поток SDK), с.
    langfuse_timeout_s: int = 30
    # Админка (ADR-0036): срок сессии после входа (без продления) и лимиты попыток входа
    # (ADR-0030; 0 — правило выключено).
    admin_session_ttl_hours: int = 12
    # Cookie сессии админки (ADR-0044) только по HTTPS. localhost браузеры считают безопасным,
    # false — только для разработки по http с другого адреса (LAN IP).
    admin_cookie_secure: bool = True
    rate_admin_login_per_ip_per_min: int = 10
    rate_admin_login_per_email_per_hour: int = 20
    # Потоки проверки паролей (scrypt ~0,1 с CPU и 32 МиБ на попытку): флуд входа ждёт в их
    # очереди, а не занимает общий пул потоков и соединения БД (REVIEW.md: AC-4).
    admin_kdf_threads: int = Field(default=2, ge=1, le=32)
    # Открытых сессий на пользователя: при входе сверх лимита закрываются самые старые.
    admin_max_sessions_per_user: int = Field(default=10, ge=1)

    @field_validator("cors_origins")
    @classmethod
    def _check_origins(cls, values: list[str]) -> list[str]:
        # «*» с credentials Starlette превращает в «любой origin читает ответы админки».
        # Значение должно совпадать с заголовком Origin побайтно: каноничная форма —
        # схема и хост в нижнем регистре, порт числом, без пути, query и учётных данных.
        for value in values:
            try:
                parts = urlsplit(value)
                port = parts.port
            except ValueError:
                parts, port = None, None
            canonical = (
                f"{parts.scheme}://{parts.hostname}{f':{port}' if port is not None else ''}"
                if parts and parts.scheme in ("http", "https") and parts.hostname
                else None
            )
            if canonical != value:
                raise ValueError(f"CORS_ORIGINS: {value!r} — не origin вида https://host[:port]")
        return values

    @field_validator("source_db_allowed_networks")
    @classmethod
    def _check_networks(cls, values: list[str]) -> list[str]:
        for value in values:
            ipaddress.ip_network(value, strict=False)
        return values

    @model_validator(mode="after")
    def _check_production(self) -> Self:
        """В production — без пароля БД по умолчанию, с паролем Redis и ключом Qdrant
        (REVIEW.md: D-1, D-2) и только https-origin (S-17): забытая переменная окружения
        не должна молча оставить настройки разработки. https://localhost допустим — локальная
        проверка прод-стека (deploy.md)."""
        if self.app_env != "production":
            return self
        password = urlsplit(self.database_url).password
        if not password or password in _DEV_DB_PASSWORDS:
            raise ValueError("DATABASE_URL: в production нужен свой пароль БД")
        if not urlsplit(self.redis_url).password:
            raise ValueError("REDIS_URL: в production нужен пароль Redis")
        if not secret_value(self.qdrant_api_key):
            raise ValueError("QDRANT_API_KEY: в production нужен ключ Qdrant")
        for origin in self.cors_origins:
            if not origin.startswith("https://"):
                raise ValueError(f"CORS_ORIGINS: {origin!r} — в production только https")
        return self

    @property
    def is_local(self) -> bool:
        return self.app_env in ("local", "test")


def secret_value(value: SecretStr | None) -> str | None:
    """Значение секрета для клиентов SDK; пустое — как не заданное."""
    return (value.get_secret_value() or None) if value is not None else None


def source_secrets_env(
    env_files: Sequence[Path | str] | None = ENV_FILES,
    environ: Mapping[str, str] = os.environ,
) -> dict[str, str]:
    """Секреты источников знаний `SOURCE_SECRET_*` (ADR-0017) из `.env` и окружения;
    окружение важнее. Полей в Settings у них нет: имена задаёт конфиг источника."""
    dotenv = DotEnvSettingsSource(Settings, env_file=env_files).env_vars if env_files else {}
    secrets = {
        name.upper(): value
        for name, value in dotenv.items()
        if value is not None and name.upper().startswith(SOURCE_SECRET_PREFIX)
    }
    secrets.update({k: v for k, v in environ.items() if k.startswith(SOURCE_SECRET_PREFIX)})
    return secrets
