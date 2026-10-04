from pathlib import Path

import pytest
from pydantic import ValidationError

from app.settings import Settings, source_secrets_env


def test_defaults_without_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("APP_ENV", raising=False)

    settings = Settings(_env_file=None)

    assert settings.app_env == "local"
    assert settings.is_local


def test_env_overrides(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("LOG_LEVEL", "WARNING")

    settings = Settings(_env_file=None)

    assert settings.app_env == "production"
    assert settings.log_level == "WARNING"
    assert not settings.is_local


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
