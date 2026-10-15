"""Структурированные логи: трейсбек в JSON без значений локальных переменных."""

import json

import pytest
import structlog

from app.logs import configure_logging
from app.settings import Settings


def _fail_with_contact() -> None:
    phone = "+7 999 111-22-33"
    raise ValueError(f"не записано ({len(phone)})")


def test_exception_log_has_traceback_without_locals(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging(Settings(_env_file=None, app_env="production"))
    try:
        try:
            _fail_with_contact()
        except ValueError:
            structlog.get_logger("test").exception("tool.failed")
        output = capsys.readouterr().out
    finally:
        configure_logging(Settings(_env_file=None, app_env="test"))

    entry = json.loads(output)
    [error] = entry["exception"]
    assert error["exc_type"] == "ValueError"
    assert any(frame["name"] == "_fail_with_contact" for frame in error["frames"])
    assert "111-22-33" not in output
    assert all("locals" not in frame for frame in error["frames"])
