"""Структурированные логи: трейсбек в JSON без значений локальных переменных."""

import json
import re
from collections.abc import MutableMapping
from typing import Any

import pytest
import structlog
from starlette.types import Message, Receive, Scope, Send

from app.logs import TraceIdMiddleware, configure_logging
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


# --- X-Trace-Id (REVIEW.md: O-6) ---


async def _trace_ids(header: str | None) -> tuple[str, str]:
    """trace_id в request.state и в заголовке ответа для входящего X-Trace-Id."""
    seen: dict[str, str] = {}

    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        seen["state"] = scope["state"]["trace_id"]
        await send({"type": "http.response.start", "status": 204, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    headers = [] if header is None else [(b"x-trace-id", header.encode())]
    sent: list[Message] = []

    async def receive() -> Message:
        return {"type": "http.request"}

    async def send(message: Message) -> None:
        sent.append(message)

    await TraceIdMiddleware(app)({"type": "http", "headers": headers}, receive, send)
    response_headers = dict(sent[0]["headers"])
    return seen["state"], response_headers[b"x-trace-id"].decode()


@pytest.mark.parametrize("value", ["trace-http", "4bf92f3577b34da6a3ce929d0e0e4736", "a.b_c-1"])
async def test_valid_trace_id_is_kept(value: str) -> None:
    assert await _trace_ids(value) == (value, value)


@pytest.mark.parametrize(
    "value",
    ["x" * 65, "x" * 200, "trace id", "trace;drop", "трейс"],
    ids=["65", "200", "space", "semicolon", "non-ascii"],
)
async def test_invalid_trace_id_is_replaced(
    value: str, debug_logs: list[MutableMapping[str, Any]]
) -> None:
    state, response = await _trace_ids(value)

    assert state == response
    assert state != value
    assert re.fullmatch(r"[0-9a-f]{32}", state)
    # В лог — только длина отвергнутого значения, не оно само.
    (rejected,) = [log for log in debug_logs if log["event"] == "trace_id_rejected"]
    assert rejected["length"] == len(value.encode().decode("latin-1"))
    assert value not in str(rejected)


async def test_missing_trace_id_is_generated() -> None:
    state, response = await _trace_ids(None)

    assert state == response and re.fullmatch(r"[0-9a-f]{32}", state)
