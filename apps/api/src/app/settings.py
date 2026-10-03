"""Настройки приложения из переменных окружения (и `.env` в корне репозитория)."""

from pathlib import Path
from typing import Literal

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

# src/app/settings.py → корень репозитория на 4 уровня выше пакета.
_REPO_ROOT = Path(__file__).resolve().parents[4]

AppEnv = Literal["local", "test", "staging", "production"]
LogLevel = Literal["DEBUG", "INFO", "WARNING", "ERROR"]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=(_REPO_ROOT / ".env", ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    app_env: AppEnv = "local"
    log_level: LogLevel = "INFO"
    database_url: str = "postgresql+asyncpg://app:app@localhost:5432/app"
    database_echo: bool = False
    redis_url: str = "redis://localhost:6379/0"
    qdrant_url: str = "http://localhost:6333"
    # Ключ виджета, который `make seed` выдаёт новому тенанту; пусто — сгенерировать.
    seed_widget_key: str | None = None
    # Origin веб-чата для CORS (env — JSON-список). Origin виджетов тенантов — P5-06.
    cors_origins: list[str] = ["http://localhost:3000"]
    # LLM-провайдеры (ADR-0010); base_url пусто — официальный endpoint.
    openai_api_key: SecretStr | None = None
    openai_base_url: str | None = None
    # Chat Completions OpenAI-совместимых API (прокси, локальные серверы).
    openai_compatible_api_key: SecretStr | None = None
    openai_compatible_base_url: str | None = None
    anthropic_api_key: SecretStr | None = None
    anthropic_base_url: str | None = None

    @property
    def is_local(self) -> bool:
        return self.app_env in ("local", "test")
