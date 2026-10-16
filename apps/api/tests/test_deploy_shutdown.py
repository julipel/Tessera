"""Остановка api в проде (REVIEW.md: O-12): Docker ждёт дольше, чем uvicorn дожидается
ходов, lifespan — прерванных ходов, плюс отправка хотя бы одной пачки трейсов Langfuse
(общего таймаута у shutdown SDK нет: LANGFUSE_TIMEOUT_S — на пачку, ADR-0029)."""

import re
from pathlib import Path

import yaml

from app.main import SHUTDOWN_TURNS_WAIT_S
from app.settings import Settings

ROOT = Path(__file__).resolve().parents[3]


def _seconds(value: str) -> int:
    match = re.fullmatch(r"(\d+)s", value)
    assert match, value
    return int(match.group(1))


def test_stop_grace_period_covers_graceful_shutdown_and_one_langfuse_batch() -> None:
    compose = yaml.safe_load((ROOT / "deploy/compose.prod.yml").read_text())
    dockerfile = (ROOT / "apps/api/Dockerfile").read_text()
    graceful = re.search(r'"--timeout-graceful-shutdown",\s*"(\d+)"', dockerfile)
    assert graceful, "uvicorn без --timeout-graceful-shutdown ждёт SSE-стримы бесконечно"

    grace = _seconds(compose["services"]["api"]["stop_grace_period"])
    langfuse_timeout = Settings.model_fields["langfuse_timeout_s"].default

    assert grace > int(graceful.group(1)) + SHUTDOWN_TURNS_WAIT_S + langfuse_timeout
