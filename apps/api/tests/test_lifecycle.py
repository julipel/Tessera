"""Закрытие ресурсов при остановке API и воркера (REVIEW.md: S-14, S-16)."""

from typing import Any

import pytest
from structlog.testing import capture_logs

from app.lifecycle import close_all
from app.worker import shutdown


async def test_close_all_continues_after_failure() -> None:
    closed: list[str] = []

    async def broken() -> None:
        raise RuntimeError("redis уже закрыт")

    async def engine() -> None:
        closed.append("engine")

    with capture_logs() as logs:
        await close_all(
            [("sync", lambda: closed.append("sync")), ("redis", broken), ("engine", engine)]
        )

    assert closed == ["sync", "engine"]
    [entry] = [e for e in logs if e["event"] == "shutdown.close_failed"]
    assert entry["resource"] == "redis"


class _Closable:
    def __init__(self, closed: list[str], name: str) -> None:
        self._closed, self._name = closed, name

    async def aclose(self) -> None:
        self._closed.append(self._name)

    async def dispose(self) -> None:
        self._closed.append(self._name)


@pytest.mark.parametrize("created", [[], ["engine"], ["engine", "web_client"]])
async def test_worker_shutdown_after_partial_startup(created: list[str]) -> None:
    """`startup` упал на полпути: закрывается только созданное, без KeyError."""
    closed: list[str] = []
    ctx: dict[str, Any] = {}
    if "engine" in created:
        ctx["engine"] = _Closable(closed, "engine")
    if "web_client" in created:
        ctx["web_client"] = type("Web", (), {"http": _Closable(closed, "web_client")})()

    await shutdown(ctx)

    assert sorted(closed) == sorted(created)
