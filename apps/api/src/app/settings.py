"""Настройки приложения из переменных окружения (и `.env` в корне репозитория)."""

import ipaddress
import os
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Literal

from pydantic import SecretStr, field_validator
from pydantic_settings import BaseSettings, DotEnvSettingsSource, SettingsConfigDict

# src/app/settings.py → корень репозитория на 4 уровня выше пакета.
_REPO_ROOT = Path(__file__).resolve().parents[4]

ENV_FILES: tuple[Path | str, ...] = (_REPO_ROOT / ".env", ".env")
SOURCE_SECRET_PREFIX = "SOURCE_SECRET_"

AppEnv = Literal["local", "test", "staging", "production"]
LogLevel = Literal["DEBUG", "INFO", "WARNING", "ERROR"]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=ENV_FILES,
        env_file_encoding="utf-8",
        extra="ignore",
    )

    app_env: AppEnv = "local"
    log_level: LogLevel = "INFO"
    database_url: str = "postgresql+asyncpg://app:app@localhost:5432/app"
    database_echo: bool = False
    redis_url: str = "redis://localhost:6379/0"
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
    # Приватные подсети, к которым можно подключать источники `database` (ADR-0018), JSON-список
    # CIDR. Публичные адреса разрешены всегда; локально — ["127.0.0.0/8"].
    source_db_allowed_networks: list[str] = []
    # Ключи виджета, которые `make seed` выдаёт новым тенантам: slug=key[,slug=key…] (или key,
    # если тенант один); у тенанта без ключа здесь — сгенерировать.
    seed_widget_key: str | None = None
    # Origin своего веб-чата (env — JSON-список): CORS и проверка Origin по ключу виджета —
    # с него ключ работает всегда, сайты тенанта задаёт allowed_origins (ADR-0022).
    cors_origins: list[str] = ["http://localhost:3000"]
    # LLM-провайдеры (ADR-0010); base_url пусто — официальный endpoint.
    openai_api_key: SecretStr | None = None
    openai_base_url: str | None = None
    # Chat Completions OpenAI-совместимых API (прокси, локальные серверы).
    openai_compatible_api_key: SecretStr | None = None
    openai_compatible_base_url: str | None = None
    anthropic_api_key: SecretStr | None = None
    anthropic_base_url: str | None = None
    # Токен API админки (`Authorization: Bearer …`) до пользователей админки (P8-01, ADR-0028);
    # без токена эндпоинты админки отвечают 404.
    admin_api_token: SecretStr | None = None

    @field_validator("source_db_allowed_networks")
    @classmethod
    def _check_networks(cls, values: list[str]) -> list[str]:
        for value in values:
            ipaddress.ip_network(value, strict=False)
        return values

    @property
    def is_local(self) -> bool:
        return self.app_env in ("local", "test")


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
