"""Настройки приложения из переменных окружения (и `.env` в корне репозитория)."""

from pathlib import Path
from typing import Literal

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
    redis_url: str = "redis://localhost:6379/0"
    qdrant_url: str = "http://localhost:6333"

    @property
    def is_local(self) -> bool:
        return self.app_env in ("local", "test")
