from pathlib import Path

import pytest
from pydantic import SecretStr, ValidationError

from app.settings import Settings, secret_value, source_secrets_env


def test_defaults_without_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("APP_ENV", raising=False)

    settings = Settings(_env_file=None)

    assert settings.app_env == "local"
    assert settings.is_local


def test_env_overrides(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("LOG_LEVEL", "WARNING")
    monkeypatch.setenv("DATABASE_URL", PROD_DATABASE_URL)
    monkeypatch.setenv("CORS_ORIGINS", '["https://chat.example.com"]')

    settings = Settings(_env_file=None)

    assert settings.app_env == "production"
    assert settings.log_level == "WARNING"
    assert not settings.is_local


PROD_DATABASE_URL = "postgresql+asyncpg://app:s3cret@postgres:5432/app"


@pytest.mark.parametrize(
    "database_url",
    [
        "postgresql+asyncpg://app:app@postgres:5432/app",
        "postgresql+asyncpg://app@postgres:5432/app",
        "postgresql+asyncpg://app:@postgres:5432/app",
    ],
)
def test_production_rejects_dev_database_password(database_url: str) -> None:
    with pytest.raises(ValidationError, match="DATABASE_URL"):
        Settings(
            _env_file=None,
            app_env="production",
            database_url=database_url,
            cors_origins=["https://chat.example.com"],
        )


@pytest.mark.parametrize("origin", ["http://localhost:3000", "http://chat.example.com"])
def test_production_rejects_http_cors_origin(origin: str) -> None:
    with pytest.raises(ValidationError, match="CORS_ORIGINS"):
        Settings(
            _env_file=None,
            app_env="production",
            database_url=PROD_DATABASE_URL,
            cors_origins=["https://chat.example.com", origin],
        )


def test_settings_error_hides_env_values() -> None:
    """Ошибка настроек не выводит значения из env — ключи и адрес БД попали бы в лог."""
    with pytest.raises(ValidationError) as error:
        Settings(
            _env_file=None,
            app_env="production",
            database_url="postgresql+asyncpg://app:app@postgres:5432/app",
            openai_api_key="sk-secret-key",
        )
    assert "sk-secret-key" not in str(error.value)
    assert "app:app@" not in str(error.value)


def test_production_allows_https_localhost() -> None:
    """Проверка прод-стека локально (deploy.md): DOMAIN=localhost."""
    settings = Settings(
        _env_file=None,
        app_env="production",
        database_url=PROD_DATABASE_URL,
        cors_origins=["https://localhost"],
    )
    assert settings.cors_origins == ["https://localhost"]


def test_dev_defaults_allowed_outside_production() -> None:
    settings = Settings(_env_file=None, app_env="staging")
    assert settings.database_url.startswith("postgresql+asyncpg://app:app@")


def test_urls_hidden_from_repr() -> None:
    settings = Settings(_env_file=None, database_url=PROD_DATABASE_URL)
    assert "s3cret" not in repr(settings)
    assert "redis://" not in repr(settings)


def test_secret_value() -> None:
    assert secret_value(None) is None
    assert secret_value(SecretStr("")) is None
    assert secret_value(SecretStr("sk")) == "sk"


def test_unknown_app_env_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("APP_ENV", "prod")

    with pytest.raises(ValidationError):
        Settings(_env_file=None)


def test_seed_widget_key_read_from_env_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("SEED_WIDGET_KEY", raising=False)
    env_file = tmp_path / ".env"
    env_file.write_text("SEED_WIDGET_KEY=wk_from_file\n")

    assert Settings(_env_file=env_file).seed_widget_key == "wk_from_file"


def test_source_secrets_from_env_file_and_environ(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("SOURCE_SECRET_A=file\nSOURCE_SECRET_B=file\nOPENAI_API_KEY=sk\n")
    environ = {"SOURCE_SECRET_B": "env", "DATABASE_URL": "pg://"}

    secrets = source_secrets_env([env_file], environ)

    assert secrets == {"SOURCE_SECRET_A": "file", "SOURCE_SECRET_B": "env"}


def test_source_db_allowed_networks_validated(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SOURCE_DB_ALLOWED_NETWORKS", '["10.20.0.0/16", "127.0.0.1/8"]')
    assert Settings(_env_file=None).source_db_allowed_networks == ["10.20.0.0/16", "127.0.0.1/8"]

    monkeypatch.setenv("SOURCE_DB_ALLOWED_NETWORKS", '["not-a-network"]')
    with pytest.raises(ValidationError):
        Settings(_env_file=None)
