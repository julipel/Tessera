"""Закрытие ресурсов процесса при остановке — для API (`main.py`) и воркера (`worker.py`)."""

from collections.abc import Awaitable, Callable, Sequence

import structlog

logger = structlog.get_logger(__name__)

type Closer = Callable[[], Awaitable[object] | object]


async def close_all(closers: Sequence[tuple[str, Closer]]) -> None:
    """Закрыть ресурсы по порядку. Сбой одного — в лог `shutdown.close_failed`, остальные
    всё равно закрываются (REVIEW.md: S-14). Закрытие может быть синхронным."""
    for name, close in closers:
        try:
            result = close()
            if isinstance(result, Awaitable):
                await result
        except Exception:
            logger.exception("shutdown.close_failed", resource=name)
